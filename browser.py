"""
Browser Manager — persistent profiles + ephemeral sessions.
Profiles stored in /app/profiles/<name>/
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional


from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import TimeoutError as PlaywrightTimeout

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
    """Single Playwright instance for ephemeral sessions + multiple persistent profiles."""

    def __init__(self, max_sessions: int = 5, login_provider=None):
        self.max_sessions = max_sessions
        self._login_provider = login_provider
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None

        self._ephemeral_contexts: Dict[str, BrowserContext] = {}
        self._ephemeral_pages: Dict[str, Dict[str, Page]] = {}

        self._profile_contexts: Dict[str, BrowserContext] = {}
        self._profile_pages: Dict[str, Dict[str, Page]] = {}

        self._created_at: Dict[str, float] = {}

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
        for pid in list(self._profile_contexts.keys()):
            try:
                await self._close_profile(pid)
            except Exception:
                pass
        for sid in list(self._ephemeral_contexts.keys()):
            try:
                await self._close_ephemeral(sid)
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
        self._ephemeral_contexts.clear()
        self._ephemeral_pages.clear()
        self._profile_contexts.clear()
        self._profile_pages.clear()
        self._created_at.clear()

    # ─────────────────────────────────────────────
    #  Identification
    # ─────────────────────────────────────────────

    def _is_profile(self, session_id: str) -> bool:
        if session_id in self._profile_contexts:
            return True
        return (PROFILES_DIR / session_id).exists()

    # ─────────────────────────────────────────────
    #  Ephemeral contexts (existing behaviour)
    # ─────────────────────────────────────────────

    async def _ensure_started(self) -> None:
        if self._browser is None:
            await self.start()

    async def _get_or_create_ephemeral(self, session_id: str) -> BrowserContext:
        if session_id in self._ephemeral_contexts:
            ctx = self._ephemeral_contexts[session_id]
            if not ctx.is_closed():
                return ctx
            await self._close_ephemeral(session_id)

        await self._enforce_ephemeral_limit(session_id)
        await self._ensure_started()

        ctx = await self._browser.new_context(  # type: ignore[union-attr]
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

    # ─────────────────────────────────────────────
    #  Persistent profiles
    # ─────────────────────────────────────────────

    async def _ensure_profile(self, profile_name: str) -> BrowserContext:
        if profile_name in self._profile_contexts:
            ctx = self._profile_contexts[profile_name]
            if not ctx.is_closed():
                return ctx
            await self._close_profile(profile_name)

        if self._pw is None:
            await self.start()

        profile_dir = PROFILES_DIR / profile_name
        profile_dir.mkdir(parents=True, exist_ok=True)

        ctx = await self._pw.chromium.launch_persistent_context(  # type: ignore[union-attr]
            user_data_dir=str(profile_dir),
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
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
        )

        if stealth_async is not None:
            try:
                await stealth_async(ctx)
            except Exception:
                pass

        self._profile_contexts[profile_name] = ctx
        self._profile_pages.setdefault(profile_name, {})
        self._created_at[profile_name] = time.time()
        return ctx

    async def _close_profile(self, profile_name: str) -> None:
        pages = self._profile_pages.pop(profile_name, {})
        for p in list(pages.values()):
            try:
                await p.close()
            except Exception:
                pass
        ctx = self._profile_contexts.pop(profile_name, None)
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass
        self._created_at.pop(profile_name, None)

    async def close_profile(self, profile_name: str) -> None:
        await self._close_profile(profile_name)

    # ─────────────────────────────────────────────
    #  Pages
    # ─────────────────────────────────────────────

    async def get_or_create_page(self, session_id: str, page_id: str = "main") -> Page:
        if self._is_profile(session_id):
            ctx = await self._ensure_profile(session_id)
            pages = self._profile_pages[session_id]
        else:
            ctx = await self._get_or_create_ephemeral(session_id)
            pages = self._ephemeral_pages[session_id]

        for pid, p in list(pages.items()):
            if p.is_closed():
                pages.pop(pid, None)

        if page_id not in pages or pages[page_id].is_closed():
            page = await ctx.new_page()
            pages[page_id] = page
            page.set_default_timeout(30000)
            page.set_default_navigation_timeout(30000)
        return pages[page_id]

    # ─────────────────────────────────────────────
    #  Persistence
    # ─────────────────────────────────────────────

    async def save_session(self, session_id: str) -> None:
        if self._is_profile(session_id):
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
            await self._close_profile(session_id)
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
        if self._login_provider is not None:
            session = await self._login_provider.start(profile_name, site)
            return {
                "session_id": session.session_id,
                "login_url": session.login_url,
                "expires_in": int(session.expires_at - time.time()) if session.expires_at else 600,
                "state": session.state.value,
            }
        # Fallback stub
        await self._ensure_profile(profile_name)
        meta = {"site": site, "created_at": time.time()}
        (PROFILES_DIR / profile_name / "meta.json").write_text(json.dumps(meta))
        return {
            "session_id": profile_name,
            "login_url": f"https://{site}",
            "expires_in": 600,
            "state": "waiting_user",
        }

    async def login_status(self, session_id: str) -> Optional[dict]:
        if self._login_provider is not None:
            session = await self._login_provider.status(session_id)
            if session:
                return {
                    "session_id": session.session_id,
                    "state": session.state.value,
                    "authenticated": session.state.value == "authenticated",
                    "login_url": session.login_url,
                }
        if self._is_profile(session_id):
            meta_path = PROFILES_DIR / session_id / "meta.json"
            site = "unknown"
            try:
                meta = json.loads(meta_path.read_text())
                site = meta.get("site", site)
            except Exception:
                pass
            return {
                "session_id": session_id,
                "state": "waiting_user",
                "authenticated": False,
                "login_url": f"https://{site}",
            }
        return None

    async def cancel_login(self, session_id: str) -> bool:
        if self._login_provider is not None:
            return await self._login_provider.cancel(session_id)
        await self._close_profile(session_id)
        return True

    # ─────────────────────────────────────────────
    #  Session management
    # ─────────────────────────────────────────────

    def list_sessions(self) -> list:
        result = []
        for sid, ctx in self._ephemeral_contexts.items():
            result.append({
                "session_id": sid,
                "type": "ephemeral",
                "created_at": self._created_at.get(sid),
            })
        for pid in self._profile_contexts:
            result.append({
                "session_id": pid,
                "type": "profile",
                "created_at": self._created_at.get(pid),
            })
        return result

    async def delete_session(self, session_id: str) -> bool:
        if session_id in self._profile_contexts:
            await self._close_profile(session_id)
            try:
                shutil.rmtree(PROFILES_DIR / session_id, ignore_errors=True)
            except Exception:
                pass
            return True
        if session_id in self._ephemeral_contexts:
            await self._close_ephemeral(session_id)
            return True
        return False

    async def refresh_session(self, session_id: str) -> dict:
        if session_id not in self._profile_contexts:
            raise RuntimeError("Profile not found")
        await self._close_profile(session_id)
        await self._ensure_profile(session_id)
        return {"ok": True, "session_id": session_id, "refreshed": True}

    async def _enforce_ephemeral_limit(self, desired_session_id: str) -> None:
        if desired_session_id in self._ephemeral_contexts:
            return
        if len(self._ephemeral_contexts) < self.max_sessions:
            return
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
