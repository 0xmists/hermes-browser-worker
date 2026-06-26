"""
browser.py — BrowserManager facade.
Routes to ProfileManager / SessionManager / LoginManager / BrowserPool.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import TimeoutError as PlaywrightTimeout

from browser_pool import BrowserPool
from login_providers import LoginProvider, LoginState
from login_manager import LoginManager
from profile_manager import ProfileManager
from session_manager import Session, SessionManager

try:
    from playwright_stealth import stealth_async  # type: ignore
except Exception:  # noqa: BLE001
    stealth_async = None  # type: ignore[assignment]

SESSIONS_DIR = Path(os.getenv("SESSIONS_DIR", "/app/sessions"))
PROFILES_DIR = Path(os.getenv("PROFILES_DIR", "/app/profiles"))
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
PROFILES_DIR.mkdir(parents=True, exist_ok=True)

MAX_PAGES_PER_CONTEXT = 2
SESSION_TIMEOUT_SECONDS = 300


class BrowserManager:
    def __init__(self, max_sessions: int = 5, login_provider: Optional[LoginProvider] = None):
        self._max_sessions = max_sessions
        self._pool = BrowserPool()
        self._profiles = ProfileManager(self._pool, PROFILES_DIR, login_provider=login_provider)
        self._sessions = SessionManager(self._profiles)
        self._logins = LoginManager(self._profiles, login_provider)

        # Ephemeral state lives here (not in Profile/Session/Login managers)
        self._ephemeral_contexts: Dict[str, BrowserContext] = {}
        self._ephemeral_pages: Dict[str, Dict[str, Page]] = {}
        self._created_at: Dict[str, float] = {}

    # ─────────────────────────────────────────────
    #  Lifecycle
    # ─────────────────────────────────────────────

    async def start(self) -> None:
        await self._pool.start()

    async def stop(self) -> None:
        await self._profiles.close_all()
        await self._close_all_ephemeral()
        await self._pool.stop()

    # ─────────────────────────────────────────────
    #  Identification
    # ─────────────────────────────────────────────

    def _is_profile(self, session_id: str) -> bool:
        if self._sessions.is_profile_session(session_id):
            return True
        return self._profiles.profile_exists(session_id)

    # ─────────────────────────────────────────────
    #  Ephemeral contexts
    # ─────────────────────────────────────────────

    async def _ensure_started(self) -> None:
        await self._pool.start()

    async def _get_or_create_ephemeral(self, session_id: str) -> BrowserContext:
        if session_id in self._ephemeral_contexts:
            ctx = self._ephemeral_contexts[session_id]
            if not ctx.is_closed():
                return ctx
            await self._close_ephemeral(session_id)

        await self._enforce_ephemeral_limit(session_id)
        await self._ensure_started()

        ctx = await self._pool.open_ephemeral_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
        )

        cookie_file = SESSIONS_DIR / f"{session_id}.json"
        if cookie_file.exists():
            try:
                data = json.loads(cookie_file.read_text())
                cookies = data.get("cookies")
                if cookies:
                    await ctx.add_cookies(cookies)
            except Exception:
                pass

        if stealth_async is not None:
            try:
                await stealth_async(ctx)
            except Exception:
                pass

        self._ephemeral_contexts[session_id] = ctx
        self._ephemeral_pages.setdefault(session_id, {})
        self._created_at[session_id] = time.time()
        self._sessions.register_ephemeral(session_id)
        return ctx

    async def _close_ephemeral(self, session_id: str) -> None:
        pages = self._ephemeral_pages.pop(session_id, {})
        for p in list(pages.values()):
            try:
                await p.close()
            except Exception:
                pass
        ctx = self._ephemeral_contexts.pop(session_id, None)
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass
        self._created_at.pop(session_id, None)

    async def _close_all_ephemeral(self) -> None:
        for sid in list(self._ephemeral_contexts.keys()):
            try:
                await self._close_ephemeral(sid)
            except Exception:
                pass

    # ─────────────────────────────────────────────
    #  Pages
    # ─────────────────────────────────────────────

    async def get_or_create_page(
        self, session_id: str, page_id: str = "main"
    ) -> Page:
        if self._is_profile(session_id):
            self._sessions.register_profile(session_id)
            return await self._profiles.get_page(session_id, page_id)

        return await self._get_or_create_ephemeral_page(session_id, page_id)

    async def _get_or_create_ephemeral_page(
        self, session_id: str, page_id: str = "main"
    ) -> Page:
        if session_id in self._ephemeral_contexts:
            ctx = self._ephemeral_contexts[session_id]
            if ctx.is_closed():
                await self._close_ephemeral(session_id)
            else:
                pages = self._ephemeral_pages.get(session_id, {})
                for pid, p in list(pages.items()):
                    if p.is_closed():
                        pages.pop(pid, None)
                if page_id in pages and not pages[page_id].is_closed():
                    return pages[page_id]

        ctx = await self._get_or_create_ephemeral(session_id)
        pages = self._ephemeral_pages[session_id]
        page = await ctx.new_page()
        pages[page_id] = page
        page.set_default_timeout(30000)
        page.set_default_navigation_timeout(30000)
        return page

    # ─────────────────────────────────────────────
    #  Persistence
    # ─────────────────────────────────────────────

    async def save_session(self, session_id: str) -> None:
        if self._is_profile(session_id):
            await self._profiles.save_state(session_id)
            return
        ctx = self._ephemeral_contexts.get(session_id)
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

    async def close_session(self, session_id: str) -> None:
        if self._is_profile(session_id):
            await self._profiles.close_profile(session_id)
            return
        await self._close_ephemeral(session_id)
        cookie_file = SESSIONS_DIR / f"{session_id}.json"
        if cookie_file.exists():
            try:
                cookie_file.unlink()
            except Exception:
                pass

    # ─────────────────────────────────────────────
    #  LoginProvider integration
    # ─────────────────────────────────────────────

    async def start_login(self, profile_name: str, site: str) -> dict:
        session = await self._logins.start(site, session_id=profile_name)
        return {
            "session_id": session.session_id,
            "login_url": session.login_url,
            "expires_in": int(session.expires_at - time.time()) if session.expires_at else 600,
            "state": session.state.value,
        }

    async def login_status(self, session_id: str) -> Optional[dict]:
        session = await self._logins.status(session_id)
        if session:
            return {
                "session_id": session.session_id,
                "state": session.state.value,
                "authenticated": session.state.value == LoginState.AUTHENTICATED.value,
                "login_url": session.login_url,
            }
        return None

    async def cancel_login(self, session_id: str) -> bool:
        return await self._logins.cancel(session_id)

    # ─────────────────────────────────────────────
    #  Session management
    # ─────────────────────────────────────────────

    def list_sessions(self) -> list:
        result = []
        for sid, ctx in self._ephemeral_contexts.items():
            result.append(
                {
                    "session_id": sid,
                    "type": "ephemeral",
                    "created_at": self._created_at.get(sid),
                }
            )
        result.extend(self._profiles.list_profiles())
        return result

    async def delete_session(self, session_id: str) -> bool:
        if self._is_profile(session_id):
            await self._profiles.close_profile(session_id)
            self._sessions.delete(session_id)
            return True
        if session_id in self._ephemeral_contexts:
            await self._close_ephemeral(session_id)
            self._sessions.delete(session_id)
            return True
        return False

    async def refresh_session(self, session_id: str) -> dict:
        if not self._profiles.profile_exists(session_id):
            raise RuntimeError("Profile not found")
        await self._profiles.close_profile(session_id)
        await self._profiles.ensure_profile(session_id)
        self._sessions.refresh(session_id)
        return {"ok": True, "session_id": session_id, "refreshed": True}

    async def _enforce_ephemeral_limit(self, desired_session_id: str) -> None:
        if desired_session_id in self._ephemeral_contexts:
            return
        if len(self._ephemeral_contexts) >= self._max_sessions:
            oldest = min(self._created_at.items(), key=lambda kv: kv[1])[0]
            await self._close_ephemeral(oldest)

    async def close_idle_sessions(self) -> None:
        now = time.time()
        for sid in list(self._ephemeral_contexts.keys()):
            pages = self._ephemeral_pages.get(sid, {})
            any_open = any(not p.is_closed() for p in pages.values()) if pages else False
            if not any_open:
                cookie_file = SESSIONS_DIR / f"{sid}.json"
                if cookie_file.exists():
                    try:
                        data = json.loads(cookie_file.read_text())
                        last = data.get("saved_at", 0)
                        if now - last > SESSION_TIMEOUT_SECONDS:
                            await self._close_ephemeral(sid)
                    except Exception:
                        pass


def session_file_exists(session_id: str) -> bool:
    return (SESSIONS_DIR / f"{session_id}.json").exists() or (PROFILES_DIR / session_id).exists()
