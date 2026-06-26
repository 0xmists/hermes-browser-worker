"""
login_providers.py — transport-agnostic login provider interface.

The Browser Worker never depends on any specific transport (CDP, noVNC,
WebSocket, WebRTC, etc.). It talks only to LoginProvider.

Phase 2 changes:
  - LoginProvider uses session objects (not profile_name + site)
  - AuthDetector strategy pattern for authentication detection
  - Transport lifecycle (expose/close) is owned by the provider
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from auth_detectors import AuthDetector


# ──────────────────────────────────────────────
#  Session state
# ──────────────────────────────────────────────

class LoginState(str, Enum):
    PENDING = "pending"
    WAITING_USER = "waiting_user"
    AUTHENTICATED = "authenticated"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass
class LoginSession:
    """A login session: ties a profile to a transport + auth state."""
    session_id: str
    profile_name: str
    state: LoginState
    site: str
    login_url: str = ""
    connect_url: Optional[str] = None
    expires_at: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    authenticated_at: Optional[float] = None
    transport_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)


# ──────────────────────────────────────────────
#  LoginProvider interface
# ──────────────────────────────────────────────

class LoginProvider(ABC):
    """
    Transport-agnostic login provider.

    Implementations:
      - PlaywrightWSProvider (Phase 2)
      - CDPProvider (future)
      - NoVNCProvider (future)
      - BrowserlessProvider (future)
    """

    @abstractmethod
    async def start(self, session: LoginSession, auth_detector: Optional[AuthDetector] = None) -> LoginSession:
        """
        Start a login session:
        1. Create/open persistent profile
        2. Expose transport (WebSocket URL, CDP endpoint, etc.)
        3. Return updated session with connect_url
        """
        raise NotImplementedError

    @abstractmethod
    async def status(self, session: LoginSession) -> LoginSession:
        """
        Check authentication status.
        Returns updated session (possibly with state=AUTHENTICATED).
        """
        raise NotImplementedError

    @abstractmethod
    async def stop(self, session: LoginSession) -> None:
        """
        Stop login: close transport, but do NOT destroy the profile.
        The profile/context survive for later reuse.
        """
        raise NotImplementedError

    async def cleanup(self, session: LoginSession) -> None:
        """
        Full cleanup: stop login + destroy profile.
        Default implementation calls stop(). Override for more cleanup.
        """
        await self.stop(session)


# ──────────────────────────────────────────────
#  Stub provider (for testing / fallback)
# ──────────────────────────────────────────────

class StubLoginProvider(LoginProvider):
    """
    Default provider for Phase 1 compatibility.
    Creates persistent profile, returns site URL as connect_url.
    Does not actually expose a transport — useful for direct browser automation.
    """

    def __init__(self, profile_manager, profiles_dir: Path):
        self.pm = profile_manager
        self.profiles_dir = profiles_dir
        self._sessions: dict[str, LoginSession] = {}

    async def start(self, session: LoginSession, auth_detector: Optional[AuthDetector] = None) -> LoginSession:
        await self.pm.ensure_profile(session.profile_name)
        session.login_url = session.site if session.site.startswith("http") else f"https://{session.site}"
        session.connect_url = session.login_url
        session.state = LoginState.WAITING_USER
        session.expires_at = time.time() + 600
        self._sessions[session.session_id] = session
        return session

    async def status(self, session: LoginSession) -> LoginSession:
        stored = self._sessions.get(session.session_id)
        if stored:
            return stored
        return session

    async def stop(self, session: LoginSession) -> None:
        self._sessions.pop(session.session_id, None)

    async def cleanup(self, session: LoginSession) -> None:
        self._sessions.pop(session.session_id, None)
        await self.pm.close_profile(session.profile_name)
