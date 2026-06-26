"""
profile_manager.py — persistent browser profile lifecycle.
One profile = one user-data-dir on disk.
Concurrent access to the same profile is serialized per-profile.
"""
from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional

from playwright.async_api import BrowserContext

from browser_pool import BrowserPool

if TYPE_CHECKING:
    from playwright.async_api import Page


class ProfileManager:
    def __init__(self, pool: BrowserPool, profiles_dir: Path, login_provider=None):
        self._pool = pool
        self._profiles_dir = profiles_dir
        self._profiles_dir.mkdir(parents=True, exist_ok=True)
        self._login_provider = login_provider
        self._contexts: Dict[str, BrowserContext] = {}
        self._pages: Dict[str, Dict[str, Page]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._created_at: Dict[str, float] = {}

    def _lock_for(self, profile_name: str) -> asyncio.Lock:
        if profile_name not in self._locks:
            self._locks[profile_name] = asyncio.Lock()
        return self._locks[profile_name]

    async def ensure_profile(self, profile_name: str) -> BrowserContext:
        lock = self._lock_for(profile_name)
        async with lock:
            if profile_name in self._contexts:
                ctx = self._contexts[profile_name]
                if not ctx.is_closed():
                    return ctx
                await self._close(profile_name)

            profile_dir = self._profiles_dir / profile_name
            profile_dir.mkdir(parents=True, exist_ok=True)

            ctx = await self._pool.open_persistent_context(profile_dir)
            self._contexts[profile_name] = ctx
            self._pages.setdefault(profile_name, {})
            self._created_at[profile_name] = time.time()
            return ctx

    async def close_profile(self, profile_name: str) -> None:
        lock = self._lock_for(profile_name)
        async with lock:
            await self._close(profile_name)

    async def delete_profile(self, profile_name: str) -> None:
        await self.close_profile(profile_name)
        profile_dir = self._profiles_dir / profile_name
        try:
            shutil.rmtree(profile_dir, ignore_errors=True)
        except Exception:
            pass
        self._locks.pop(profile_name, None)

    async def get_page(self, profile_name: str, page_id: str = "main") -> Page:
        ctx = await self.ensure_profile(profile_name)
        pages = self._pages[profile_name]
        for pid, p in list(pages.items()):
            if p.is_closed():
                pages.pop(pid, None)
        if page_id not in pages or pages[page_id].is_closed():
            page = await ctx.new_page()
            pages[page_id] = page
            page.set_default_timeout(30000)
            page.set_default_navigation_timeout(30000)
        return pages[page_id]

    async def save_state(self, profile_name: str) -> None:
        # Persistent profiles save state automatically via profile_dir.
        pass

    async def close_all(self) -> None:
        for name in list(self._contexts.keys()):
            try:
                await self._close(name)
            except Exception:
                pass

    async def _close(self, profile_name: str) -> None:
        pages = self._pages.pop(profile_name, {})
        for p in list(pages.values()):
            try:
                await p.close()
            except Exception:
                pass
        ctx = self._contexts.pop(profile_name, None)
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass
        self._created_at.pop(profile_name, None)

    def is_profile(self, profile_name: str) -> bool:
        return (
            profile_name in self._contexts
            or (self._profiles_dir / profile_name).exists()
        )

    def list_profiles(self) -> list:
        result = []
        for pid, ctx in self._contexts.items():
            result.append(
                {
                    "session_id": pid,
                    "type": "profile",
                    "created_at": self._created_at.get(pid),
                }
            )
        return result

    def profile_exists(self, profile_name: str) -> bool:
        return self.is_profile(profile_name)
