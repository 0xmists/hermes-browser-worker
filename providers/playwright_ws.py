"""
playwright_ws.py — Login viewer WebSocket relay server.

Architecture:
    Browser (Playwright)
        │
        │ screenshots + CDP events
        ▼
    LoginViewerServer
        │
        │ WebSocket (binary frames + JSON commands)
        ▼
    Browser-accessible login page (canvas + input forwarding)

The server:
    - Generates temporary login tokens (not tied to session_id)
    - Serves the login page at GET /login/{token}
    - Relays browser screenshots to client as JPEG frames
    - Relays client input (mouse, keyboard, touch, scroll) to Playwright
    - Enforces TTL (auto-close after expiry)
    - Invalidates token after auth detection
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
JPEG_QUALITY = 60  # screenshot quality (1-100)
SCREENSHOT_INTERVAL_MS = 200  # 5 fps


@dataclass
class ViewerSession:
    """Tracks a login viewer session."""
    token: str
    session_id: str
    context: "BrowserContext"
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    ws: Optional[object] = None  # WebSocket connection
    screenshot_task: Optional[asyncio.Task] = None
    closed: bool = False
    authenticated: bool = False

    def __post_init__(self):
        if self.expires_at == 0.0:
            self.expires_at = self.created_at + 600


class LoginViewerServer:
    """
    WebSocket relay server for browser login viewer.

    Each login session gets a unique token. The flow:
        1. Create ViewerSession with token
        2. Serve login page at GET /login/{token}
        3. Client connects WebSocket
        4. Server streams screenshots to client
        5. Client sends input events, server forwards to Playwright
        6. On auth detection: close viewer, invalidate token
    """

    def __init__(self):
        self._viewers: Dict[str, ViewerSession] = {}  # token -> ViewerSession
        self._token_to_session: Dict[str, str] = {}  # token -> session_id
        self._lock = asyncio.Lock()

    async def create_viewer(
        self,
        session_id: str,
        context: "BrowserContext",
        ttl: int = 600,
    ) -> str:
        """
        Create a login viewer for a browser context.

        Returns a login token. The viewer URL is /login/{token}.
        """
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
            self._token_to_session[token] = session_id

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

    async def connect_ws(self, token: str, ws) -> Optional[ViewerSession]:
        """Register a WebSocket connection to a viewer."""
        viewer = self._viewers.get(token)
        if viewer is None or viewer.closed:
            return None

        # Close existing connection if any
        if viewer.ws is not None:
            try:
                await viewer.ws.close()
            except Exception:
                pass

        viewer.ws = ws
        logger.info("WebSocket connected for token %s", token[:8])

        # Start screenshot streaming
        if viewer.screenshot_task is None or viewer.screenshot_task.done():
            viewer.screenshot_task = asyncio.create_task(
                self._stream_screenshots(viewer)
            )

        return viewer

    async def disconnect_ws(self, token: str) -> None:
        """Disconnect WebSocket but keep viewer alive for reconnect."""
        viewer = self._viewers.get(token)
        if viewer:
            viewer.ws = None

    async def close_viewer(self, token: str) -> None:
        """Close a viewer session completely."""
        async with self._lock:
            viewer = self._viewers.pop(token, None)
            self._token_to_session.pop(token, None)

        if viewer is None:
            return

        viewer.closed = True

        # Stop screenshot task
        if viewer.screenshot_task and not viewer.screenshot_task.done():
            viewer.screenshot_task.cancel()

        # Close WebSocket
        if viewer.ws is not None:
            try:
                await viewer.ws.close()
            except Exception:
                pass

        logger.info("Closed viewer for token %s", token[:8])

    async def invalidate_token(self, token: str) -> None:
        """Invalidate token after authentication (keep profile, close viewer)."""
        viewer = self._viewers.get(token)
        if viewer:
            viewer.authenticated = True
        await self.close_viewer(token)

    async def _stream_screenshots(self, viewer: ViewerSession) -> None:
        """Continuously capture and send screenshots to the client."""
        try:
            while not viewer.closed and viewer.ws is not None:
                # Get the first page
                pages = viewer.context.pages
                if not pages:
                    await asyncio.sleep(0.5)
                    continue

                page = pages[0]

                try:
                    # Capture screenshot as JPEG
                    img_bytes = await page.screenshot(
                        type="jpeg",
                        quality=JPEG_QUALITY,
                    )
                    img_b64 = base64.b64encode(img_bytes).decode("ascii")

                    # Get page dimensions
                    dimensions = await page.evaluate("""() => ({
                        width: window.innerWidth,
                        height: window.innerHeight,
                        devicePixelRatio: window.devicePixelRatio,
                    })""")

                    if viewer.ws is not None and not viewer.closed:
                        await viewer.ws.send_json({
                            "type": "frame",
                            "data": img_b64,
                            "width": dimensions["width"],
                            "height": dimensions["height"],
                        })
                except Exception as e:
                    logger.debug("Screenshot error: %s", e)

                await asyncio.sleep(SCREENSHOT_INTERVAL_MS / 1000)

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Screenshot stream error: %s", e)

    async def handle_client_message(
        self,
        token: str,
        message: dict,
    ) -> None:
        """Process an input event from the client."""
        viewer = self._viewers.get(token)
        if viewer is None or viewer.closed:
            return

        pages = viewer.context.pages
        if not pages:
            return

        page = pages[0]
        msg_type = message.get("type")

        try:
            if msg_type == "mouse":
                # {type: "mouse", x: N, y: N, action: "move|down|up", button: 0}
                x, y = message["x"], message["y"]
                action = message.get("action", "move")
                button = message.get("button", 0)

                # Scale coordinates to page dimensions
                box = await page.evaluate("""(coords) => {
                    const el = document.querySelector('canvas');
                    if (!el) return null;
                    const rect = el.getBoundingClientRect();
                    return {left: rect.left, top: rect.top, width: rect.width, height: rect.height};
                }""", {"x": x, "y": y})

                if box:
                    page_x = box["left"] + (x * box["width"])
                    page_y = box["top"] + (y * box["height"])

                    if action == "move":
                        await page.mouse.move(page_x, page_y)
                    elif action == "down":
                        await page.mouse.down(button=button)
                    elif action == "up":
                        await page.mouse.up(button=button)
                    elif action == "click":
                        await page.mouse.click(page_x, page_y, button=button)

            elif msg_type == "wheel":
                # {type: "wheel", dx: N, dy: N}
                dx = message.get("dx", 0)
                dy = message.get("dy", 0)
                await page.mouse.wheel(dx, dy)

            elif msg_type == "key":
                # {type: "key", key: "Enter", code: "Enter", modifiers: 0}
                key = message.get("key", "")
                code = message.get("code", "")
                modifiers = message.get("modifiers", 0)

                if code:
                    await page.keyboard.press(code)
                elif key:
                    await page.keyboard.press(key)

            elif msg_type == "type":
                # {type: "type", text: "hello"}
                text = message.get("text", "")
                await page.keyboard.type(text)

            elif msg_type == "navigate":
                # {type: "navigate", url: "https://..."}
                url = message.get("url", "")
                if url:
                    await page.goto(url, wait_until="domcontentloaded")

            elif msg_type == "touch":
                # {type: "touch", x: N, y: N, action: "start|move|end"}
                # x, y are normalized (0-1), scale to page dimensions
                x_norm, y_norm = message["x"], message["y"]
                action = message.get("action", "move")

                # Scale to page
                box = await page.evaluate("""() => ({
                    w: window.innerWidth,
                    h: window.innerHeight
                })""")
                x = x_norm * box["w"]
                y = y_norm * box["h"]

                if action in ("start", "move"):
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

    def get_stats(self) -> dict:
        """Get server statistics."""
        return {
            "active_viewers": len([v for v in self._viewers.values() if not v.closed]),
            "total_tokens": len(self._viewers),
        }


# ──────────────────────────────────────────────
#  Global singleton
# ──────────────────────────────────────────────

_viewer_server = LoginViewerServer()


def get_viewer_server() -> LoginViewerServer:
    """Get the global viewer server singleton."""
    return _viewer_server
