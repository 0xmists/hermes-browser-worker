"""
playwright_ws_provider.py — LoginProvider implementation with browser viewer.

This provider:
    1. Creates a persistent browser profile
    2. Exposes the browser via LoginViewerServer (web-based viewer)
    3. Returns a login URL (https) for the user to open
    4. Monitors for authentication via AuthDetector
    5. On auth: invalidates token, closes viewer
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from login_providers import LoginProvider, LoginSession, LoginState
from auth_detectors import AuthDetector
from providers.playwright_ws import get_viewer_server

if TYPE_CHECKING:
    from profile_manager import ProfileManager

logger = logging.getLogger("browser-worker.ws-provider")


class PlaywrightWSProvider(LoginProvider):
    """
    LoginProvider implementation that exposes persistent profiles
    via a web-based browser viewer.
    """

    def __init__(self, profile_manager: "ProfileManager", profiles_dir: Path):
        self.pm = profile_manager
        self.profiles_dir = profiles_dir
        self._active: dict[str, LoginSession] = {}  # session_id -> LoginSession
        self._auth_tasks: dict[str, asyncio.Task] = {}  # session_id -> Task

    async def start(
        self,
        session: LoginSession,
        auth_detector: Optional[AuthDetector] = None,
    ) -> LoginSession:
        try:
            await self.pm.ensure_profile(session.profile_name)

            viewer_server = get_viewer_server()
            token = await viewer_server.create_viewer(
                session.session_id,
                await self.pm.get_context(session.profile_name),
                ttl=600,
            )

            session.login_url = session.site if session.site.startswith("http") else f"https://{session.site}"
            session.connect_url = f"/login/{token}"
            session.state = LoginState.WAITING_USER
            session.expires_at = __import__("time").time() + 600
            session.transport_id = token

            self._active[session.session_id] = session

            if auth_detector:
                task = asyncio.create_task(
                    self._monitor_auth(session.session_id, auth_detector, token)
                )
                self._auth_tasks[session.session_id] = task

            logger.info(
                "Started login session %s (token=%s, url=%s)",
                session.session_id[:16], token[:8], session.connect_url,
            )
            return session
        except Exception as e:
            logger.error("PlaywrightWSProvider.start failed: %s", e, exc_info=True)
            session.login_url = session.site if session.site.startswith("http") else f"https://{session.site}"
            session.state = LoginState.WAITING_USER
            session.expires_at = __import__("time").time() + 600
            self._active[session.session_id] = session
            return session

    async def status(self, session: LoginSession) -> LoginSession:
        stored = self._active.get(session.session_id)
        if stored:
            return stored
        return session

    async def stop(self, session: LoginSession) -> None:
        viewer_server = get_viewer_server()
        viewer_server.close_viewer(session.transport_id) if session.transport_id else None
        task = self._auth_tasks.pop(session.session_id, None)
        if task and not task.done():
            task.cancel()
        self._active.pop(session.session_id, None)
        logger.info("Stopped login transport for %s", session.session_id[:16])

    async def cleanup(self, session: LoginSession) -> None:
        await self.stop(session)
        await self.pm.close_profile(session.profile_name)
        logger.info("Cleaned up login session %s", session.session_id[:16])

    async def _monitor_auth(
        self,
        session_id: str,
        detector: AuthDetector,
        token: str,
    ) -> None:
        """Background task: monitor for authentication and invalidate token."""
        try:
            import time as _time
            viewer_server = get_viewer_server()
            context = viewer_server.get_context(token)

            if context is None:
                logger.error("No context for token %s", token[:8])
                return

            while session_id in self._active:
                pages = context.pages
                page = pages[0] if pages else None
                if page is None:
                    await asyncio.sleep(0.5)
                    continue

                result = await detector.check(context, page)

                if result.authenticated:
                    session = self._active.get(session_id)
                    if session:
                        session.state = LoginState.AUTHENTICATED
                        session.authenticated_at = _time.time()
                        logger.info("Session %s authenticated: %s", session_id[:16], result.reason)

                    # Invalidate token (closes viewer, keeps profile)
                    await viewer_server.invalidate_token(token)

                    # Notify connected clients
                    v = viewer_server.get_viewer(token)
                    if v and v.ws and not v.closed:
                        try:
                            await v.ws.send_json({"type": "authenticated"})
                        except Exception:
                            pass
                    return

                await asyncio.sleep(2.0)

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Auth monitor error for %s: %s", session_id[:16], e)
