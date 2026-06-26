"""
session_manager.py — session to profile mapping and session metadata.
Does NOT own browser resources.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Session:
    session_id: str
    profile_name: Optional[str] = None
    type: str = "ephemeral"
    created_at: float = field(default_factory=time.time)


class SessionManager:
    def __init__(self, profile_manager):
        self._profile_mgr = profile_manager
        self._sessions: Dict[str, Session] = {}

    def register_ephemeral(self, session_id: str) -> Session:
        if session_id not in self._sessions:
            self._sessions[session_id] = Session(session_id=session_id)
        return self._sessions[session_id]

    def register_profile(self, profile_name: str) -> Session:
        if profile_name not in self._sessions:
            self._sessions[profile_name] = Session(
                session_id=profile_name,
                profile_name=profile_name,
                type="profile",
            )
        return self._sessions[profile_name]

    def get(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    def resolve_profile(self, session_id: str) -> Optional[str]:
        sess = self._sessions.get(session_id)
        if sess and sess.type == "profile":
            return sess.profile_name
        return None

    def delete(self, session_id: str) -> bool:
        if session_id in self._sessions:
            del self._sessions[session_id]
            return True
        return False

    def refresh(self, session_id: str) -> bool:
        if session_id not in self._sessions:
            return False
        self._sessions[session_id].created_at = time.time()
        return True

    def list_sessions(self) -> List[Session]:
        return list(self._sessions.values())

    def is_profile_session(self, session_id: str) -> bool:
        sess = self._sessions.get(session_id)
        if sess:
            return sess.type == "profile"
        return self._profile_mgr.profile_exists(session_id)
