"""
novnc.py — VNC-over-WebSocket transport (placeholder).

Future implementation:
    Expose browser desktop via noVNC / websockify
    Connect via WebSocket to the VNC endpoint
    Authentication detected by polling screen content
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext


class NoVNCTransport:
    """VNC-over-WebSocket transport — placeholder for future implementation."""

    async def expose(self, context: "BrowserContext", session_id: str) -> str:
        raise NotImplementedError("noVNC transport not yet implemented")

    async def close(self, session_id: str) -> None:
        pass


novnc = NoVNCTransport
