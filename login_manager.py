"""
login_manager.py — login state machine and provider orchestration.
Does NOT own browser resources.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Dict, Optional

from login_providers import LoginProvider, LoginSession, LoginState
from profile_manager import ProfileManager

if TYPE_CHECKING:
    from auth_detectors import AuthDetectorRegistry

logger = logging.getLogger("browser-worker.login-mgr")


class LoginManager:
    """
    Orchestrates login sessions using a LoginProvider.

    Flow:
        1. Hermes calls start_login(site) -> creates LoginSession
        2. Provider exposes transport, returns connect_url
        3. User interacts with browser via transport
        4. Hermes polls login/status until authenticated
        5. Hermes calls cancel_login when done
    """

    def __init__(self, profile_mgr: ProfileManager, login_provider: Optional[LoginProvider] = None,
                 auth_registry: Optional["AuthDetectorRegistry"] = None):
        self._profile_mgr = profile_mgr
        self._provider = login_provider
        self._sessions: Dict[str, LoginSession] = {}
        self._auth_registry = auth_registry

    async def start(self, site: str, session_id: Optional[str] = None) -> LoginSession:
        """Start a new login session for the given site."""
        if self._provider:
            profile_name = session_id or site.replace(".", "_").replace("/", "_")
            session = LoginSession(
                session_id=profile_name,
                profile_name=profile_name,
                state=LoginState.PENDING,
                site=site,
                login_url=site if site.startswith("http") else f"https://{site}",
            )
            session = await self._provider.start(session)
        else:
            # No provider configured — create a stub session
            profile_name = session_id or site.replace(".", "_").replace("/", "_")
            session = LoginSession(
                session_id=profile_name,
                profile_name=profile_name,
                state=LoginState.WAITING_USER,
                site=site,
                login_url=site if site.startswith("http") else f"https://{site}",
                expires_at=time.time() + 600,
            )

        self._sessions[session.session_id] = session
        return session

    async def status(self, session_id: str) -> Optional[LoginSession]:
        """Get the current state of a login session."""
        if self._provider:
            session = await self._provider.status(
                LoginSession(
                    session_id=session_id,
                    profile_name=session_id,
                    state=LoginState.PENDING,
                    site="",
                )
            )
            if session:
                self._sessions[session_id] = session
                return session
        return self._sessions.get(session_id)

    async def cancel(self, session_id: str) -> bool:
        """Cancel a login session (does NOT destroy the profile)."""
        if self._provider:
            await self._provider.stop(
                LoginSession(
                    session_id=session_id,
                    profile_name=session_id,
                    state=LoginState.CANCELLED,
                    site="",
                )
            )
            self._sessions.pop(session_id, None)
            return True
        session = self._sessions.pop(session_id, None)
        if session:
            await self._profile_mgr.close_profile(session.profile_name)
            return True
        return False

    async def cleanup(self, session_id: str) -> None:
        """Full cleanup: cancel login + destroy profile."""
        if self._provider:
            await self._provider.cleanup(
                LoginSession(
                    session_id=session_id,
                    profile_name=session_id,
                    state=LoginState.CANCELLED,
                    site="",
                )
            )
        self._sessions.pop(session_id, None)

    def register_local(self, session: LoginSession) -> None:
        """Register a session from an external source."""
        self._sessions[session.session_id] = session

    def get_authenticated(self, session_id: str) -> Optional[LoginSession]:
        """Get a session only if it's authenticated."""
        session = self._sessions.get(session_id)
        if session and session.state == LoginState.AUTHENTICATED:
            return session
        return None

    def force_authenticate(self, session_id: str) -> bool:
        """Manually mark a session as authenticated (for ManualDetector)."""
        session = self._sessions.get(session_id)
        if session:
            session.state = LoginState.AUTHENTICATED
            session.authenticated_at = time.time()
            return True
        return False
