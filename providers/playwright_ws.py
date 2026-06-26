"""
playwright_ws.py — Playwright WebSocket transport.

Exposes a browser context over WebSocket so that external clients
(CLI tools, IDEs, other agents) can interact with the persistent profile
in real time.

Architecture:
    Client (Hermes/CLI)
        │
        │ WebSocket (ws://host/ws?session=xxx&token=yyy)
        ▼
    PlaywrightWSServer
        │
        │ wraps
        ▼
    BrowserContext (persistent profile)

The server:
    - Authenticates via token in query string
    - Associates each connection with a session
    - Enforces expiration (auto-close after TTL)
    - Cleans up idle connections
    - Supports reconnects (same session = same profile)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, Optional, Set

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

logger = logging.getLogger("browser-worker.ws")

WS_HEARTBEAT_INTERVAL = 25  # seconds
SESSION_DIR = os.getenv("SESSIONS_DIR", "/app/sessions")


@dataclass
class WSConnection:
    """Tracks a single WebSocket connection to a session."""
    session_id: str
    token: str
    connected_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    authenticated: bool = False


class PlaywrightWSServer:
    """
    WebSocket server that exposes Playwright browser contexts.

    Each session gets a unique token. Clients connect with:
        ws://host/ws?session=<session_id>&token=<token>

    The server relays commands (navigate, click, fill, etc.) from the
    client to the Playwright context and sends back results.
    """

    def __init__(self):
        self._connections: Dict[str, WSConnection] = {}
        self._session_tokens: Dict[str, str] = {}
        self._contexts: Dict[str, "BrowserContext"] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._expiry: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    def generate_token(self, session_id: str) -> str:
        """Generate a secure token for a session."""
        import secrets
        token = secrets.token_urlsafe(32)
        self._session_tokens[session_id] = token
        return token

    def validate_token(self, session_id: str, token: str) -> bool:
        """Validate a session token."""
        return self._session_tokens.get(session_id) == token

    async def expose_context(
        self,
        session_id: str,
        context: "BrowserContext",
        ttl: int = 600,
    ) -> str:
        """
        Expose a browser context over WebSocket.

        Returns the WebSocket URL for the client to connect to.
        """
        async with self._lock:
            token = self.generate_token(session_id)
            self._contexts[session_id] = context
            self._expiry[session_id] = time.time() + ttl

            ws_url = f"/ws?session={session_id}&token={token}"
            logger.info("Exposed session %s (TTL=%ds)", session_id[:16], ttl)
            return ws_url

    async def close_session(self, session_id: str) -> None:
        """Close a session's WebSocket connection and clean up."""
        async with self._lock:
            self._connections.pop(session_id, None)
            self._session_tokens.pop(session_id, None)
            self._contexts.pop(session_id, None)
            self._expiry.pop(session_id, None)
            task = self._tasks.pop(session_id, None)
            if task and not task.done():
                task.cancel()
            logger.info("Closed session %s", session_id[:16])

    def is_active(self, session_id: str) -> bool:
        """Check if a session is still active."""
        if session_id not in self._expiry:
            return False
        return time.time() < self._expiry[session_id]

    def get_context(self, session_id: str) -> Optional["BrowserContext"]:
        """Get the browser context for a session."""
        return self._contexts.get(session_id)

    def register_connection(self, conn: WSConnection) -> None:
        """Register a new WebSocket connection."""
        self._connections[conn.session_id] = conn

    def unregister_connection(self, session_id: str) -> None:
        """Unregister a WebSocket connection (but keep context alive)."""
        self._connections.pop(session_id, None)

    def get_active_sessions(self) -> list:
        """Get list of active session IDs."""
        now = time.time()
        return [sid for sid, exp in self._expiry.items() if now < exp]

    async def cleanup_expired(self) -> int:
        """Remove expired sessions. Returns count of cleaned sessions."""
        now = time.time()
        expired = [sid for sid, exp in self._expiry.items() if now >= exp]
        for sid in expired:
            await self.close_session(sid)
        return len(expired)


# ──────────────────────────────────────────────
#  Global singleton
# ──────────────────────────────────────────────

_ws_server = PlaywrightWSServer()


def get_ws_server() -> PlaywrightWSServer:
    """Get the global WebSocket server singleton."""
    return _ws_server
