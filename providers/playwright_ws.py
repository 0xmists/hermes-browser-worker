"""
playwright_ws.py — Login viewer using SSE + HTTP POST (WebSocket alternative).

Since Railway's proxy doesn't support WebSocket upgrades, this module uses:
- Server-Sent Events (SSE) for streaming screenshots to the client
- HTTP POST /login-input/{token} for receiving input events from the client

This works over standard HTTP and is compatible with Railway's proxy.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

logger = logging.getLogger("browser-worker.viewer")

SESSION_DIR = os.getenv("SESSIONS_DIR", "/app/sessions")
JPEG_QUALITY = 55  # screenshot quality (1-100)
SCREENSHOT_INTERVAL_MS = 250  # 4 fps


@dataclass
class ViewerSession:
    """Tracks a login viewer session."""
    token: str
    session_id: str
    context: "BrowserContext"
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    authenticated: bool = False
    closed: bool = False
    screenshot_task: Optional[asyncio.Task] = None
    input_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    _latest_frame: Optional[dict] = field(default=None, init=False, repr=False)

    def __post_init__(self):
        if self.expires_at == 0.0:
            self.expires_at = self.created_at + 600


class LoginViewerServer:
    """
    Server-Sent Events relay server for browser login viewer.

    Each login session gets a unique token. The flow:
        1. Create ViewerSession with token
        2. Serve login page at GET /login/{token}
        3. Client opens SSE connection at GET /login-stream/{token}
        4. Server streams screenshots to client via SSE
        5. Client sends input via POST /login-input/{token}
        6. On auth detection: close viewer, invalidate token
    """

    def __init__(self):
        self._viewers: Dict[str, ViewerSession] = {}  # token -> ViewerSession
        self._lock = asyncio.Lock()

    async def create_viewer(
        self,
        session_id: str,
        context: "BrowserContext",
        ttl: int = 600,
    ) -> str:
        """Create a login viewer. Returns a login token."""
        import secrets
        token = secrets.token_urlsafe(24)

        viewer = ViewerSession(
            token=token,
            session_id=session_id,
            context=context,
            expires_at=time.time() + ttl,
        )

        async with self._lock:
            self._viewers[token] = viewer

        logger.info("Created viewer for session %s (token=%s)", session_id[:16], token[:8])
        return token

    def get_viewer(self, token: str) -> Optional[ViewerSession]:
        """Get a viewer session by token."""
        return self._viewers.get(token)

    def get_context(self, token: str) -> Optional["BrowserContext"]:
        """Get the browser context for a token."""
        viewer = self._viewers.get(token)
        if viewer:
            return viewer.context
        return None

    def validate_token(self, token: str) -> bool:
        """Check if a token is valid and not expired."""
        viewer = self._viewers.get(token)
        if viewer is None:
            return False
        if viewer.closed:
            return False
        if time.time() > viewer.expires_at:
            return False
        return True

    async def close_viewer(self, token: str) -> None:
        """Close a viewer session completely."""
        async with self._lock:
            viewer = self._viewers.pop(token, None)

        if viewer is None:
            return

        viewer.closed = True

        # Stop screenshot task
        if viewer.screenshot_task and not viewer.screenshot_task.done():
            viewer.screenshot_task.cancel()

        logger.info("Closed viewer for token %s", token[:8])

    async def invalidate_token(self, token: str) -> None:
        """Invalidate token after authentication."""
        viewer = self._viewers.get(token)
        if viewer:
            viewer.authenticated = True
        await self.close_viewer(token)

    async def enqueue_input(self, token: str, data: dict) -> bool:
        """Queue an input event from the client."""
        viewer = self._viewers.get(token)
        if viewer is None or viewer.closed:
            return False
        await viewer.input_queue.put(data)
        return True

    async def start_screenshot_stream(self, token: str):
        """Start streaming screenshots for a viewer. Returns async generator."""
        viewer = self._viewers.get(token)
        if viewer is None or viewer.closed:
            return

        viewer.screenshot_task = asyncio.create_task(
            self._capture_screenshots(viewer)
        )

    async def _capture_screenshots(self, viewer: ViewerSession) -> None:
        """Continuously capture screenshots and put them in the stream."""
        try:
            while not viewer.closed:
                pages = viewer.context.pages
                if not pages:
                    await asyncio.sleep(0.5)
                    continue

                page = pages[0]

                try:
                    img_bytes = await page.screenshot(
                        type="jpeg",
                        quality=JPEG_QUALITY,
                    )
                    img_b64 = base64.b64encode(img_bytes).decode("ascii")

                    dimensions = await page.evaluate("""() => ({
                        width: window.innerWidth,
                        height: window.innerHeight,
                    })""")

                    if viewer.closed:
                        return

                    # Store latest frame for SSE pickup
                    viewer._latest_frame = {
                        "data": img_b64,
                        "width": dimensions["width"],
                        "height": dimensions["height"],
                        "timestamp": time.time(),
                    }
                except Exception as e:
                    logger.debug("Screenshot error: %s", e)

                await asyncio.sleep(SCREENSHOT_INTERVAL_MS / 1000)

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Screenshot stream error: %s", e)

    async def process_input(self, token: str, data: dict) -> None:
        """Process an input event from the client."""
        viewer = self._viewers.get(token)
        if viewer is None or viewer.closed:
            return

        pages = viewer.context.pages
        if not pages:
            return

        page = pages[0]
        msg_type = data.get("type")

        try:
            if msg_type == "mouse":
                x_norm, y_norm = data["x"], data["y"]
                action = data.get("action", "move")
                button = data.get("button", 0)

                box = await page.evaluate("""() => ({
                    w: window.innerWidth, h: window.innerHeight
                })""")
                x = x_norm * box["w"]
                y = y_norm * box["h"]

                if action == "move":
                    await page.mouse.move(x, y)
                elif action == "down":
                    await page.mouse.down(button=button)
                elif action == "up":
                    await page.mouse.up(button=button)
                elif action == "click":
                    await page.mouse.click(x, y, button=button)

            elif msg_type == "wheel":
                dx = data.get("dx", 0)
                dy = data.get("dy", 0)
                await page.mouse.wheel(dx, dy)

            elif msg_type == "key":
                code = data.get("code", "")
                key = data.get("key", "")
                if code:
                    await page.keyboard.press(code)
                elif key:
                    await page.keyboard.press(key)

            elif msg_type == "type":
                text = data.get("text", "")
                await page.keyboard.type(text)

            elif msg_type == "navigate":
                url = data.get("url", "")
                if url:
                    await page.goto(url, wait_until="domcontentloaded")

            elif msg_type == "touch":
                x_norm, y_norm = data["x"], data["y"]
                box = await page.evaluate("""() => ({
                    w: window.innerWidth, h: window.innerHeight
                })""")
                x = x_norm * box["w"]
                y = y_norm * box["h"]
                await page.touchscreen.tap(x, y)

        except Exception as e:
            logger.debug("Input handling error: %s", e)

    async def cleanup_expired(self) -> int:
        """Remove expired viewer sessions."""
        now = time.time()
        expired = [
            token for token, viewer in self._viewers.items()
            if now > viewer.expires_at and not viewer.closed
        ]
        for token in expired:
            await self.close_viewer(token)
        return len(expired)


# ──────────────────────────────────────────────
#  Global singleton
# ──────────────────────────────────────────────

_viewer_server = LoginViewerServer()


def get_viewer_server() -> LoginViewerServer:
    """Get the global viewer server singleton."""
    return _viewer_server
