"""
Transport implementations for Browser Worker.

Phase 2: Playwright WebSocket (playwright_ws.py)
Future: Chrome DevTools Protocol (cdp.py), noVNC (novnc.py), WebRTC, Browserless

Each transport implements:
    async def expose(context, session) -> str  # returns connect URL
    async def close(session) -> None
"""
