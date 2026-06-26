"""
playwright_ws_provider.py — LoginProvider implementation using Playwright WebSocket.

This provider:
    1. Creates a persistent browser profile
    2. Exposes the profile's browser context over WebSocket
    3. Returns a connect_url for clients to attach
    4. Monitors for authentication via AuthDetector
    5. Supports reconnects (same session = same profile)
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from login_providers import LoginProvider, LoginSession, LoginState
from auth_detectors import AuthDetector
from providers.playwright_ws import get_ws_server

if TYPE_CHECKING:
    from profile_manager import ProfileManager

logger = logging.getLogger("browser-worker.ws-provider")


class PlaywrightWSProvider(LoginProvider):
    """
    LoginProvider implementation that exposes persistent profiles
    via WebSocket for direct browser interaction.
    """

    def __init__(self, profile_manager: "ProfileManager", profiles_dir: Path):
        self.pm = profile_manager
        self.profiles_dir = profiles_dir
        self._active: dict[str, LoginSession] = {}
        self._auth_tasks: dict[str, asyncio.Task] = {}

    async def start(
        self,
        session: LoginSession,
        auth_detector: Optional[AuthDetector] = None,
    ) -> LoginSession:
        """
        Start a login session:
        1. Open/create persistent profile
        2. Expose context over WebSocket
        3. Start auth monitoring (if detector provided)
        4. Return session with connect_url
        """
        try:
            # Ensure profile exists
            await self.pm.ensure_profile(session.profile_name)

            # Get the context for auth monitoring
            ctx = await self.pm.get_context(session.profile_name)
            if ctx is None:
                raise RuntimeError(f"Failed to get context for profile {session.profile_name}")

            # Expose via WebSocket
            ws_server = get_ws_server()
            connect_path = await ws_server.expose_context(
                session.session_id,
                ctx,
                ttl=600,
            )

            # Build full connect_url (will be filled by app.py with host info)
            session.connect_url = connect_path
            session.state = LoginState.WAITING_USER
            session.expires_at = time.time() + 600
            session.transport_id = session.session_id

            self._active[session.session_id] = session

            # Start auth monitoring in background
            if auth_detector:
                task = asyncio.create_task(
                    self._monitor_auth(session.session_id, ctx, auth_detector)
                )
                self._auth_tasks[session.session_id] = task

            logger.info(
                "Started login session %s for %s (connect_url=%s)",
                session.session_id[:16],
                session.site,
                connect_path,
            )
            return session
        except Exception as e:
            logger.error("PlaywrightWSProvider.start failed: %s", e, exc_info=True)
            # Fall back to stub behavior — return session without transport
            session.login_url = session.site if session.site.startswith("http") else f"https://{session.site}"
            session.state = LoginState.WAITING_USER
            session.expires_at = time.time() + 600
            self._active[session.session_id] = session
            return session

    async def status(self, session: LoginSession) -> LoginSession:
        """Check current auth status."""
        stored = self._active.get(session.session_id)
        if stored:
            return stored
        return session

    async def stop(self, session: LoginSession) -> None:
        """
        Stop login: close transport, but keep profile alive.
        The profile/context survive for later reuse.
        """
        ws_server = get_ws_server()
        ws_server.unregister_connection(session.session_id)

        # Cancel auth monitoring
        task = self._auth_tasks.pop(session.session_id, None)
        if task and not task.done():
            task.cancel()

        # Remove from active but DON'T close the profile
        self._active.pop(session.session_id, None)
        logger.info("Stopped login transport for %s", session.session_id[:16])

    async def cleanup(self, session: LoginSession) -> None:
        """Full cleanup: stop transport + destroy profile."""
        await self.stop(session)
        await self.pm.close_profile(session.profile_name)
        logger.info("Cleaned up login session %s", session.session_id[:16])

    async def _monitor_auth(
        self,
        session_id: str,
        context: "BrowserContext",
        detector: AuthDetector,
    ) -> None:
        """Background task: monitor for authentication completion."""
        try:
            while session_id in self._active:
                # Get the first page (or create one)
                pages = context.pages
                page = pages[0] if pages else await context.new_page()

                result = await detector.check(context, page)

                if result.authenticated:
                    session = self._active.get(session_id)
                    if session:
                        session.state = LoginState.AUTHENTICATED
                        session.authenticated_at = time.time()
                        logger.info(
                            "Session %s authenticated: %s",
                            session_id[:16],
                            result.reason,
                        )
                    return

                await asyncio.sleep(2.0)

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Auth monitor error for %s: %s", session_id[:16], e)
