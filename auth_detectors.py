"""
auth_detectors.py — authentication detection strategies.

Each detector inspects a browser context/page and determines whether
the user has completed authentication.

Strategies:
  — URL change: wait for URL to match a pattern
  — Cookie presence: check for specific cookies
  — Required selector: wait for a DOM element to appear
  — Custom callback: user-provided async function
  — Manual confirmation: Hermes polls and tells us when done
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional, Protocol

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page


class AuthSignal(Protocol):
    """Return value of an auth detector."""
    authenticated: bool
    reason: str


@dataclass
class AuthResult:
    """Result of an authentication check."""
    authenticated: bool
    reason: str = ""
    metadata: dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}

    @classmethod
    def wait(cls) -> AuthResult:
        return cls(authenticated=False, reason="waiting")

    @classmethod
    def yes(cls, reason: str = "authenticated", **meta) -> AuthResult:
        return cls(authenticated=True, reason=reason, metadata=meta)

    @classmethod
    def no(cls, reason: str = "not_authenticated", **meta) -> AuthResult:
        return cls(authenticated=False, reason=reason, metadata=meta)


class AuthDetector(ABC):
    """Base class for authentication detection strategies."""

    @abstractmethod
    async def check(self, context: "BrowserContext", page: "Page") -> AuthResult:
        """Check if the user is authenticated."""
        raise NotImplementedError


class URLChangeDetector(AuthDetector):
    """Detect auth by watching for URL changes to a target pattern."""

    def __init__(self, target_pattern: str, timeout_ms: int = 30000):
        self.pattern = re.compile(target_pattern)
        self.timeout_ms = timeout_ms

    async def check(self, context: "BrowserContext", page: "Page") -> AuthResult:
        current = page.url
        if self.pattern.search(current):
            return AuthResult.yes(f"url_matched: {current}", url=current)
        return AuthResult.wait()


class CookiePresenceDetector(AuthDetector):
    """Detect auth by checking for specific cookies."""

    def __init__(self, cookie_names: list[str], domain: Optional[str] = None):
        self.cookie_names = cookie_names
        self.domain = domain

    async def check(self, context: "BrowserContext", page: "Page") -> AuthResult:
        cookies = await context.cookies()
        found = [c["name"] for c in cookies if c["name"] in self.cookie_names]
        if found:
            return AuthResult.yes(
                f"cookies_found: {', '.join(found)}",
                cookies=found,
            )
        return AuthResult.wait()


class SelectorDetector(AuthDetector):
    """Detect auth by waiting for a DOM element to appear."""

    def __init__(self, selector: str, timeout_ms: int = 30000):
        self.selector = selector
        self.timeout_ms = timeout_ms

    async def check(self, context: "BrowserContext", page: "Page") -> AuthResult:
        try:
            el = await page.wait_for_selector(
                self.selector,
                state="visible",
                timeout=self.timeout_ms,
            )
            if el:
                return AuthResult.yes(f"selector_found: {self.selector}")
        except Exception:
            pass
        return AuthResult.wait()


class CallbackDetector(AuthDetector):
    """Detect auth using a custom async callback."""

    def __init__(self, callback: Callable):
        self.callback = callback

    async def check(self, context: "BrowserContext", page: "Page") -> AuthResult:
        try:
            result = await self.callback(context, page)
            if isinstance(result, bool):
                return AuthResult.yes("callback_true") if result else AuthResult.wait()
            if isinstance(result, AuthResult):
                return result
            return AuthResult.wait()
        except Exception as e:
            return AuthResult.no(f"callback_error: {e}")


class ManualDetector(AuthDetector):
    """
    Manual confirmation — always returns wait().
    Hermes polls login/status until the user confirms externally.
    The state is updated by POST /login/status with {force_authenticated: true}.
    """

    async def check(self, context: "BrowserContext", page: "Page") -> AuthResult:
        return AuthResult.wait()


class CompositeDetector(AuthDetector):
    """OR-combination: any detector returning yes means authenticated."""

    def __init__(self, detectors: list[AuthDetector]):
        self.detectors = detectors

    async def check(self, context: "BrowserContext", page: "Page") -> AuthResult:
        for d in self.detectors:
            result = await d.check(context, page)
            if result.authenticated:
                return result
        return AuthResult.wait()


# ──────────────────────────────────────────────
#  Registry: per-site detectors
# ──────────────────────────────────────────────

class AuthDetectorRegistry:
    """Maps site patterns to auth detectors."""

    def __init__(self):
        self._detectors: list[tuple[str, AuthDetector]] = []

    def register(self, site_pattern: str, detector: AuthDetector) -> None:
        """Register a detector for sites matching the pattern."""
        self._detectors.append((site_pattern, detector))

    def detector_for(self, site: str) -> Optional[AuthDetector]:
        """Get the detector for a site, or None if no match."""
        for pattern, detector in self._detectors:
            if re.search(pattern, site):
                return detector
        return None

    def register_defaults(self) -> None:
        """Register common site detectors."""
        # GitHub: check for user avatar
        self.register(
            r"(www\.)?github\.com",
            SelectorDetector("img.avatar.circle", timeout_ms=15000),
        )
        # Google: check for account avatar
        self.register(
            r"(www\.)?google\.com",
            SelectorDetector("a[href*='myaccount']", timeout_ms=15000),
        )
        # Twitter/X: check for primary column
        self.register(
            r"(www\.)?(twitter|x)\.com",
            SelectorDetector("[data-testid='primaryColumn']", timeout_ms=15000),
        )
