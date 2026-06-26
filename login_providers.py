"""
LoginProvider abstraction for Browser Worker login flows.

Transport-agnostic interface. Implementations: stub, CDP, noVNC, Browserless, etc.
Changing remote viewing technology requires changing only this file.
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class LoginState(str, Enum):
    PENDING = "pending"
    WAITING_USER = "waiting_user"
    AUTHENTICATED = "authenticated"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass
class LoginSession:
    session_id: str
    profile_name: str
    state: LoginState
    login_url: Optional[str] = None
    expires_at: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    authenticated_at: Optional[float] = None


class LoginProvider(ABC):
    """Transport-agnostic login provider."""

    @abstractmethod
    async def start(self, profile_name: str, site: str) -> LoginSession:
        raise NotImplementedError

    @abstractmethod
    async def status(self, session_id: str) -> Optional[LoginSession]:
        raise NotImplementedError

    @abstractmethod
    async def cancel(self, session_id: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def cleanup(self, session_id: str) -> None:
        raise NotImplementedError


class StubLoginProvider(LoginProvider):
    """Default provider: launches persistent profile, returns site URL."""

    def __init__(self, browser_manager, profiles_dir: Path):
        self.bm = browser_manager
        self.profiles_dir = profiles_dir
        self._sessions: dict[str, LoginSession] = {}

    async def start(self, profile_name: str, site: str) -> LoginSession:
        session_id = profile_name
        await self.bm._ensure_profile(profile_name)
        meta = {"site": site, "created_at": time.time()}
        (self.profiles_dir / session_id / "meta.json").write_text(
            json.dumps(meta)
        )
        session = LoginSession(
            session_id=session_id,
            profile_name=profile_name,
            state=LoginState.WAITING_USER,
            login_url=f"https://{site}",
            expires_at=time.time() + 600,
        )
        self._sessions[session_id] = session
        return session

    async def status(self, session_id: str) -> Optional[LoginSession]:
        return self._sessions.get(session_id)

    async def cancel(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        if session:
            await self.bm.close_profile(session.profile_name)
            return True
        return False

    async def cleanup(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        await self.bm.close_profile(session_id)
