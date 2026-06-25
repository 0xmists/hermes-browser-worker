"""
Browser Manager — single Playwright instance reused across requests.
Sessions stored in /sessions/<session_id>.json

Usage:
    from browser import get_manager
    mgr = get_manager()
    ctx, created = await mgr.get_or_create_context("session-abc")
"""

import json
import os
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

SESSIONS_DIR = Path("/app/sessions")
SESSIONS_DIR.mkdir(exist_ok=True)

MAX_PAGES_PER_SESSION = 2
SESSION_TIMEOUT_SECONDS = 300


class BrowserManager:
    """
    Singleton-ish browser controller.
    - One browser instance at a time (headless Chromium).
    - Multiple contexts for isolation.
    - Pages are created per-request and reused by session.
    """

    def __init__(self):
        self._pw = None
        self._browser: Optional[Browser] = None
        self._contexts: dict[str, BrowserContext] = {}
        self._pages: dict[str, dict[str, Page]] = {}   # context_id -> pages

    async def start(self):
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
                "--single-process",   # critical for 1 GB RAM
                "--disable-setuid-sandbox",
            ],
        )

    async def stop(self):
        for ctx_pages in self._pages.values():
            for page in ctx_pages.values():
                try:
                    await page.close()
                except Exception:
                    pass
        for ctx in self._contexts.values():
            try:
                await ctx.close()
            except Exception:
                pass
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._browser = None
        self._pw = None
        self._contexts.clear()
        self._pages.clear()

    async def get_or_create_context(self, session_id: str, app_manager: BrowserManager) -> tuple:
        """
        Get an existing context for this session or create one with loaded cookies.
        Pass `app_manager` (the global _manager from app.py) to avoid circular imports.
        """
        # Reuse context if alive
        if session_id in app_manager._contexts:
            ctx = app_manager._contexts[session_id]
            # check if still usable
            try:
                pages = app_manager._pages[session_id]
                for cid, p in list(pages.items()):
                    if p.is_closed():
                        pages.pop(cid, None)
                ctx = app_manager._contexts[session_id]
            except Exception:
                app_manager._contexts.pop(session_id, None)
                app_manager._pages.pop(session_id, None)
                ctx = None
            if ctx:
                return ctx, False

        # Create new context
        browser = app_manager._browser
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            locale="en-US",
            timezone_id="America/New_York",
        )

        # Load saved cookies if any
        cookie_file = SESSIONS_DIR / f"{session_id}.json"
        if cookie_file.exists():
            try:
                data = json.loads(cookie_file.read_text())
                if data.get("cookies"):
                    await ctx.add_cookies(data["cookies"])
            except Exception:
                pass

        self._contexts[session_id] = ctx
        self._pages[session_id] = {}
        return ctx, True

    async def get_or_create_page(self, session_id: str, page_id: str = "main") -> Page:
        ctx, _ = await self.get_or_create_context(session_id, self)
        pages = self._pages[session_id]

        if page_id not in pages or pages[page_id].is_closed():
            page = await ctx.new_page()
            pages[page_id] = page
        return pages[page_id]

    async def save_session(self, session_id: str):
        if session_id not in self._contexts:
            return
        ctx = self._contexts[session_id]
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

    async def close_session(self, session_id: str):
        await self.save_session(session_id)
        if session_id in self._pages:
            for p in self._pages[session_id].values():
                try:
                    await p.close()
                except Exception:
                    pass
            del self._pages[session_id]
        if session_id in self._contexts:
            try:
                await self._contexts[session_id].close()
            except Exception:
                pass
            del self._contexts[session_id]

    async def close_idle_sessions(self):
        """Optional periodic cleanup — call from a background task."""
        now = time.time()
        for sid in list(self._contexts.keys()):
            ctx = self._contexts[sid]
            pages = self._pages.get(sid, {})
            idle_ok = all(p.is_closed() for p in pages.values()) if pages else True
            if idle_ok and paths(sid):
                try:
                    data = json.loads(paths(sid).read_text())
                    last = data.get("saved_at", 0)
                    if now - last > SESSION_TIMEOUT_SECONDS:
                        await self.close_session(sid)
                except Exception:
                    pass


def paths(session_id: str) -> Optional[Path]:
    f = SESSIONS_DIR / f"{session_id}.json"
    return f if f.exists() else None
