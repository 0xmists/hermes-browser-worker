"""
browser_pool.py — raw Playwright lifecycle and context factory.
No session/login logic.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright


class BrowserPool:
    def __init__(self) -> None:
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._browser is not None:
            return
        async with self._start_lock:
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
        async with self._start_lock:
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
            if self._pw:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
                self._pw = None

    async def open_persistent_context(
        self, profile_dir: Path, **kwargs
    ) -> BrowserContext:
        # launch_persistent_context manages its own browser + playwright instance
        if self._pw is None:
            self._pw = await async_playwright().start()
        return await self._pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=True,
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
            **kwargs,
        )

    async def open_ephemeral_context(self, **kwargs) -> BrowserContext:
        await self.start()
        return await self._browser.new_context(  # type: ignore[union-attr]
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
            **kwargs,
        )

    async def close_context(self, ctx: BrowserContext) -> None:
        try:
            await ctx.close()
        except Exception:
            pass
