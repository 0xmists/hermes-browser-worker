"""
cdp.py — Chrome DevTools Protocol transport (placeholder).

Future implementation:
    Expose browser via CDP endpoint (e.g., localhost:9222)
    Connect via WebSocket to the CDP endpoint
    Authentication detected by polling CDP events
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext


class CDPTransport:
    """Chrome DevTools Protocol transport — placeholder for future implementation."""

    async def expose(self, context: "BrowserContext", session_id: str) -> str:
        raise NotImplementedError("CDP transport not yet implemented")

    async def close(self, session_id: str) -> None:
        pass


cdn = CDPTransport
