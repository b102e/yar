#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from brain.agent import Agent
from brain.autonomous_research import AutonomousResearch, InterestManager
from brain.autonomy import AutonomyManager
from brain.continuity import ContinuityTracker
from brain.episodic_memory import EpisodicMemory
from brain.memory import Memory
from brain.memory_consolidation import MemoryConsolidation
from brain.memory_search import MemorySearch
from brain.self_check import SelfCheck
from brain.state import InternalState
from brain.upgrade_proposals import UpgradeProposals
from identity.keypair import check_permissions, load_or_create

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# One Agent instance per player_id (in-memory registry)
_agents: dict[str, Agent] = {}
_agent_locks: dict[str, asyncio.Lock] = {}


@dataclass
class SessionAuth:
    player_id: str
    expires_at: datetime


_sessions: dict[str, SessionAuth] = {}
TOKEN_TTL_HOURS = int(os.environ.get("YAR_TOKEN_TTL_HOURS", "24") or "24")


def _safe_player_id(player_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", str(player_id or "").strip())
    return safe or "default"


def _access_password() -> str:
    return os.environ.get("YAR_ACCESS_PASSWORD", "").strip()


def _default_player_id() -> str:
    return os.environ.get("YAR_DEFAULT_PLAYER_ID", "TRENOINFINITO").strip() or "TRENOINFINITO"


def _check_access_password(raw: str) -> bool:
    expected = _access_password()
    provided = str(raw or "")
    if not expected:
        return False
    return hmac.compare_digest(provided, expected)


def _prune_sessions() -> None:
    now = datetime.now(timezone.utc)
    expired = [token for token, item in _sessions.items() if item.expires_at <= now]
    for token in expired:
        _sessions.pop(token, None)


def _create_session(player_id: str) -> tuple[str, datetime]:
    _prune_sessions()
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=max(1, TOKEN_TTL_HOURS))
    _sessions[token] = SessionAuth(player_id=player_id, expires_at=expires_at)
    return token, expires_at


def _extract_bearer_token(authorization: str | None) -> str:
    raw = str(authorization or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return ""


def _get_player_from_auth(authorization: str | None) -> str:
    _prune_sessions()
    token = _extract_bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    session = _sessions.get(token)
    if not session:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    if session.expires_at <= datetime.now(timezone.utc):
        _sessions.pop(token, None)
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return session.player_id


def _build_agent(player_id: str) -> Agent:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    identity = load_or_create()
    for warn in check_permissions():
        print(warn)

    base_agent_dir = Path(os.environ.get("AGENT_DIR", "~/.agent")).expanduser()
    player_memory_dir = base_agent_dir / "players" / _safe_player_id(player_id) / "memory"

    memory = Memory(base_dir=str(player_memory_dir), identity=identity)
    autonomy = AutonomyManager(memory.memory_dir, identity=identity)
    interest_manager = InterestManager(memory.memory_dir)
    consolidation = MemoryConsolidation(
        memory_dir=memory.memory_dir,
        interest_manager=interest_manager,
        identity=identity,
    )
    memory.set_consolidation(consolidation)
    continuity = ContinuityTracker(memory.memory_dir, identity=identity)
    checker = SelfCheck(memory.memory_dir, consolidation=consolidation, identity=identity)
    proposals = UpgradeProposals(memory.memory_dir, identity=identity)

    memory_search = MemorySearch(memory.memory_dir, identity=identity)
    memory.set_search(memory_search)

    episodic = EpisodicMemory(
        memory.memory_dir,
        api_key=api_key,
        identity=identity,
    )
    memory.set_episodic(episodic)

    research = AutonomousResearch(
        memory_dir=memory.memory_dir,
        api_key=api_key,
        daily_token_limit=50000,
        interest_manager=interest_manager,
    )
    research.set_memory_search(memory_search)

    state = InternalState()
    agent = Agent(
        memory=memory,
        state=state,
        continuity=continuity,
        self_check=checker,
        proposals=proposals,
        memory_search=memory_search,
        episodic=episodic,
        research=research,
        consolidation=consolidation,
        autonomy=autonomy,
        identity=identity,
    )
    checker.set_agent(agent)
    checker.run()
    return agent


def get_agent(player_id: str) -> Agent:
    pid = _safe_player_id(player_id)
    if pid not in _agents:
        _agents[pid] = _build_agent(pid)
        _agent_locks[pid] = asyncio.Lock()
    return _agents[pid]


class LoginRequest(BaseModel):
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str
    expires_at: str
    player_id: str


class MessageRequest(BaseModel):
    player_id: str
    message: str


class MessageResponse(BaseModel):
    response: str
    player_id: str


class DieRequest(BaseModel):
    confirm: bool
    reason: str = ""


class DieResponse(BaseModel):
    certificate: str
    player_id: str


async def _ask_agent(agent: Agent, message: str) -> str:
    loop = asyncio.get_running_loop()
    response_future: asyncio.Future[str] = loop.create_future()

    async def sink(event: dict):
        if event.get("type") == "message" and event.get("role") == "assistant":
            content = str(event.get("content", "")).strip()
            if content and not response_future.done():
                response_future.set_result(content)

    agent.set_event_sink(sink)
    await agent.process_external_text(message)

    if not response_future.done():
        fallback = ""
        for entry in reversed(getattr(agent.memory, "short_term", [])):
            if entry.get("role") == "assistant":
                raw = str(entry.get("content", "")).strip()
                fallback = agent._strip_commands(raw) if raw else ""
                break
        response_future.set_result(fallback)

    return await response_future


@app.post("/v1/auth/login", response_model=LoginResponse)
async def auth_login(req: LoginRequest):
    if not _access_password():
        raise HTTPException(status_code=500, detail="YAR_ACCESS_PASSWORD is not set")
    if not _check_access_password(req.password):
        raise HTTPException(status_code=401, detail="invalid password")

    player_id = _default_player_id()
    token, expires_at = _create_session(player_id)
    return LoginResponse(
        access_token=token,
        token_type="bearer",
        expires_at=expires_at.isoformat(),
        player_id=player_id,
    )


@app.post("/v1/message", response_model=MessageResponse)
async def send_message(req: MessageRequest, authorization: str | None = Header(default=None)):
    try:
        token_player_id = _get_player_from_auth(authorization)

        player_id = req.player_id.strip()
        message = req.message.strip()
        if not player_id:
            raise HTTPException(status_code=400, detail="player_id is required")
        if not message:
            raise HTTPException(status_code=400, detail="message is required")
        if player_id != token_player_id:
            raise HTTPException(status_code=403, detail="player_id mismatch")

        agent = get_agent(player_id)
        lock = _agent_locks[_safe_player_id(player_id)]
        async with lock:
            response = await _ask_agent(agent, message)

        return MessageResponse(response=response, player_id=player_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
    _prune_sessions()
    return {
        "status": "ok",
        "agents": len(_agents),
        "auth": bool(_access_password()),
        "sessions": len(_sessions),
    }



@app.post("/v1/die", response_model=DieResponse)
async def agent_die(req: DieRequest, authorization: str | None = Header(default=None)):
    if not req.confirm:
        raise HTTPException(status_code=400, detail="confirm must be true to execute death protocol")

    player_id = _get_player_from_auth(authorization)
    pid = _safe_player_id(player_id)

    identity = load_or_create()
    if identity.is_dead():
        raise HTTPException(status_code=409, detail="agent is already dead")

    agent = _agents.get(pid)
    if agent is None:
        raise HTTPException(status_code=404, detail="no active agent for this player")

    lock = _agent_locks.get(pid)
    async def _run_die():
        from lifecycle.die import die
        return die(identity, reason=req.reason or "", memory=agent.memory)

    loop = asyncio.get_running_loop()
    try:
        if lock:
            async with lock:
                cert_path = await loop.run_in_executor(None, lambda: __import__("lifecycle.die", fromlist=["die"]).die(identity, reason=req.reason or "", memory=agent.memory))
        else:
            cert_path = await loop.run_in_executor(None, lambda: __import__("lifecycle.die", fromlist=["die"]).die(identity, reason=req.reason or "", memory=agent.memory))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Remove dead agent from cache
    _agents.pop(pid, None)
    _agent_locks.pop(pid, None)

    try:
        cert_txt = cert_path.with_suffix(".txt").read_text(encoding="utf-8")
    except Exception:
        cert_txt = "Death protocol complete. Certificate not readable."

    return DieResponse(certificate=cert_txt, player_id=player_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
