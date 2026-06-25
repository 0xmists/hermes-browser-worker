"""
Browser Manager — single Playwright instance reused across requests.
Sessions stored in /sessions/<session_id>.json

This module provides a robust, memory-efficient browser automation layer:

- One browser instance at a time (headless Chromium)
- Multiple contexts for session isolation
- Pages are created per-request and reused by session/id
- Automatic cookie and storage state persistence
- Session cap enforcement (close oldest when max reached)
- Hard per-page timeouts to prevent indefinite hangs
- Graceful cleanup on errors and shutdown
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, Optional, Tuple
from weakref import WeakValueDictionary

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import TimeoutError as PlaywrightTimeout

SESSIONS_DIR = Path(os.getenv("SESSIONS_DIR", "/app/sessions"))
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# Defaults - can be overridden by BrowserManager(max_sessions=...)
MAX_PAGES_PER_SESSION = 2
SESSION_TIMEOUT_SECONDS = 300


class BrowserManager:
    """
    Singleton-ish browser controller.
    """

    def __init__(self, max_sessions: int = 5):
        self.max_sessions = max_sessions
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._contexts: Dict[str, BrowserContext] = {}
        self._pages: Dict[str, Dict[str, Page]] = {}
        self._session_created: Dict[str, float] = {}

    # ─────────────────────────────────────────────
    #  Lifecycle
    # ─────────────────────────────────────────────

    async def start(self) -> None:
        if self._browser is not None:
            return
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-extensions",
                "--disable-background-networking",
            ],
        )

    async def stop(self) -> None:
        # Try best-effort cleanup of all pages first
        for ctx_pages in self._pages.values():
            for page in ctx_pages.values():
                try:
                    await page.close()
                except Exception:
                    pass
        for ctx in list(self._contexts.values()):
            try:
                await ctx.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._browser = None
        self._pw = None
        self._contexts.clear()
        self._pages.clear()
        self._session_created.clear()

    # ─────────────────────────────────────────────
    #  Session enforcement
    # ─────────────────────────────────────────────

    async def _enforce_session_limit(self, desired_session_id: str) -> None:
        if desired_session_id in self._contexts:
            return
        if len(self._contexts) < self.max_sessions:
            return
        oldest = min(self._session_created, key=self._session_created.get)
        await self.close_session(oldest)

    # ─────────────────────────────────────────────
    #  Contexts
    # ─────────────────────────────────────────────

    async def get_or_create_context(self, session_id: str) -> Tuple[BrowserContext, bool]:
        existing = self._contexts.get(session_id)
        if existing:
            pages = self._pages.get(session_id, {})
            for pid, p in list(pages.items()):
                if p.is_closed():
                    pages.pop(pid, None)
            if session_id in self._contexts:
                return existing, False

        await self._enforce_session_limit(session_id)

        browser = self._browser
        if browser is None:
            raise RuntimeError("BrowserManager not started. Call start() first.")

        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
        )

        # Restore cookies if persisted
        cookie_file = SESSIONS_DIR / f"{session_id}.json"
        if cookie_file.exists():
            try:
                data = json.loads(cookie_file.read_text())
                cookies = data.get("cookies")
                if cookies:
                    await ctx.add_cookies(cookies)
            except Exception:
                pass

        self._contexts[session_id] = ctx
        self._pages.setdefault(session_id, {})
        self._session_created[session_id] = time.time()
        return ctx, True

    # ─────────────────────────────────────────────
    #  Pages
    # ─────────────────────────────────────────────

    async def get_or_create_page(self, session_id: str, page_id: str = "main") -> Page:
        ctx, _ = await self.get_or_create_context(session_id)
        pages = self._pages[session_id]

        if page_id not in pages or pages[page_id].is_closed():
            page = await ctx.new_page()
            pages[page_id] = page
            # Hard timeouts so nothing can hang indefinitely
            page.set_default_timeout(30000)
            page.set_default_navigation_timeout(30000)
        return pages[page_id]

    # ─────────────────────────────────────────────
    #  Persistence
    # ─────────────────────────────────────────────

    async def save_session(self, session_id: str) -> None:
        ctx = self._contexts.get(session_id)
        if ctx is None:
            return
        cookie_file = SESSIONS_DIR / f"{session_id}.json"
        try:
            cookies = await ctx.cookies()
            state = {
                "cookies": cookies,
                "storage_state": None,
                "saved_at": int(time.time()),
            }
            cookie_file.write_text(json.dumps(state, indent=2, default=str))
        except Exception:
            pass

    # ─────────────────────────────────────────────
    #  Teardown
    # ─────────────────────────────────────────────

    async def close_session(self, session_id: str) -> None:
        await self.save_session(session_id)

        pages = self._pages.get(session_id)
        if pages:
            for p in list(pages.values()):
                try:
                    await p.close()
                except Exception:
                    pass
            del self._pages[session_id]

        ctx = self._contexts.pop(session_id, None)
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass
        self._session_created.pop(session_id, None)

    async def close_idle_sessions(self) -> None:
        now = time.time()
        for sid in list(self._contexts.keys()):
            pages = self._pages.get(sid, {})
            any_open = any(not p.is_closed() for p in pages.values()) if pages else False
            if not any_open:
                cookie_file = SESSIONS_DIR / f"{sid}.json"
                if cookie_file.exists():
                    try:
                        data = json.loads(cookie_file.read_text())
                        last = data.get("saved_at", 0)
                        if now - last > SESSION_TIMEOUT_SECONDS:
                            await self.close_session(sid)
                    except Exception:
                        pass


def session_file_exists(session_id: str) -> bool:
    return (SESSIONS_DIR / f"{session_id}.json").exists()
