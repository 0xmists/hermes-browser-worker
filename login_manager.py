"""
login_manager.py — login state machine and provider orchestration.
Does NOT own browser resources.
"""
from __future__ import annotations

import time
from typing import Dict, Optional

from login_providers import LoginState, LoginSession, LoginProvider

from profile_manager import ProfileManager


class LoginManager:
    def __init__(self, profile_mgr: ProfileManager, login_provider: Optional[LoginProvider]):
        self._profile_mgr = profile_mgr
        self._provider = login_provider
        self._sessions: Dict[str, LoginSession] = {}

    async def start(self, site: str, session_id: Optional[str] = None) -> LoginSession:
        profile_name = session_id or site
        if self._provider:
            session = await self._provider.start(profile_name, site)
        else:
            await self._profile_mgr.ensure_profile(profile_name)
            session = LoginSession(
                session_id=profile_name,
                profile_name=profile_name,
                state=LoginState.WAITING_USER,
                login_url=f"https://{site}",
                expires_at=time.time() + 600,
            )
        self._sessions[session.session_id] = session
        return session

    async def status(self, session_id: str) -> Optional[LoginSession]:
        if self._provider:
            session = await self._provider.status(session_id)
            if session:
                self._sessions[session_id] = session
                return session
        return self._sessions.get(session_id)

    async def cancel(self, session_id: str) -> bool:
        if self._provider:
            return await self._provider.cancel(session_id)
        session = self._sessions.pop(session_id, None)
        if session:
            await self._profile_mgr.close_profile(session.profile_name)
            return True
        return False

    async def cleanup(self, session_id: str) -> None:
        if self._provider:
            await self._provider.cleanup(session_id)
        self._sessions.pop(session_id, None)

    def register_local(self, session: LoginSession) -> None:
        self._sessions[session.session_id] = session
