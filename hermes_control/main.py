"""FastAPI entry point — wires routes to the docker + state readers."""
import asyncio
import json
import socket
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from hermes_control import __version__
from hermes_control.auth import require_bearer
from hermes_control.chat import ChatRequest, run_chat
from hermes_control.config import config
from hermes_control import docker_ctl, state_reader

app = FastAPI(title="hermes-control", version=__version__)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Unauth'd liveness probe. Safe to expose — no sensitive data."""
    summary = await docker_ctl.summary(config.container_name)
    return {
        "status": "ok",
        "version": __version__,
        "host": socket.gethostname(),
        "uptime_s": state_reader.sidecar_uptime_s(),
        "container_exists": summary.get("exists", False),
        "container_running": summary.get("running", False),
    }


@app.get("/status", dependencies=[Depends(require_bearer)])
async def status() -> dict[str, Any]:
    """Aggregate dashboard payload — one request populates the full agent card."""
    container, cron, db, cfg, orphans = await asyncio.gather(
        docker_ctl.summary(config.container_name),
        asyncio.to_thread(state_reader.read_cron_jobs),
        state_reader.state_db_probe(),
        asyncio.to_thread(state_reader.config_summary),
        docker_ctl.list_orphans(config.container_name),
    )
    sessions = await asyncio.to_thread(state_reader.list_sessions, 10, 0)
    return {
        "container": container,
        "hermes": {
            "paused": container.get("paused", False),
            "last_tick_at": cron.get("last_tick_at"),
            "recent_sessions": sessions,
        },
        "cron": {
            "job_count": len(cron.get("jobs", [])),
            "jobs": cron.get("jobs", []),
        },
        "state_db": db,
        "config": cfg,
        "orphans": orphans,
    }


@app.get("/sessions", dependencies=[Depends(require_bearer)])
async def sessions(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(state_reader.list_sessions, limit, offset)


@app.get("/sessions/{session_id}", dependencies=[Depends(require_bearer)])
async def session_detail(session_id: str) -> dict[str, Any]:
    data = await asyncio.to_thread(state_reader.read_session, session_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} not found")
    return data


@app.get("/cron", dependencies=[Depends(require_bearer)])
async def cron() -> dict[str, Any]:
    return await asyncio.to_thread(state_reader.read_cron_jobs)


@app.get("/logs", dependencies=[Depends(require_bearer)])
async def logs(
    tail: int = Query(500, ge=1),
    since: str | None = Query(None, description="Docker --since expression, e.g. 10m or 2026-04-18T12:00:00"),
) -> dict[str, Any]:
    tail = min(tail, config.max_log_tail)
    lines = await docker_ctl.logs(config.container_name, tail=tail, since=since)
    return {"lines": lines, "tail": tail, "since": since}


@app.post("/pause", dependencies=[Depends(require_bearer)])
async def pause() -> dict[str, Any]:
    r = await docker_ctl.pause(config.container_name)
    if not r["ok"]:
        raise HTTPException(status_code=502, detail=r.get("stderr") or "docker pause failed")
    return r


@app.post("/resume", dependencies=[Depends(require_bearer)])
async def resume() -> dict[str, Any]:
    r = await docker_ctl.unpause(config.container_name)
    if not r["ok"]:
        raise HTTPException(status_code=502, detail=r.get("stderr") or "docker unpause failed")
    return r


@app.post("/restart", dependencies=[Depends(require_bearer)])
async def restart() -> dict[str, Any]:
    r = await docker_ctl.restart(config.container_name)
    if not r["ok"]:
        raise HTTPException(status_code=502, detail=r.get("stderr") or "docker restart failed")
    return r


@app.get("/orphans", dependencies=[Depends(require_bearer)])
async def orphans() -> list[dict[str, Any]]:
    return await docker_ctl.list_orphans(config.container_name)


@app.post("/orphans/prune", dependencies=[Depends(require_bearer)])
async def orphans_prune() -> dict[str, Any]:
    return await docker_ctl.prune_orphans(config.container_name)


class ChatBody(BaseModel):
    prompt: str
    session_id: str | None = None
    max_turns: int = 30
    skills: list[str] = []
    source: str = "crucible"


@app.post("/chat", dependencies=[Depends(require_bearer)])
async def chat(body: ChatBody) -> StreamingResponse:
    """Stream a one-shot hermes chat turn as SSE.

    Body:
      prompt       — user message
      session_id   — pass the sid from a previous /chat response to continue the conversation
      max_turns    — cap hermes's tool-call iterations (default 30)
      skills       — optional list of skill names to preload for this turn
      source       — session tag (default 'crucible')
    """
    req = ChatRequest(
        prompt=body.prompt,
        session_id=body.session_id,
        max_turns=body.max_turns,
        skills=body.skills,
        source=body.source,
    )

    async def _stream():
        # 2KB pad so every chunk gets flushed through any proxy
        pad = ":" + (" " * 2048) + "\n"
        async for evt in run_chat(req):
            yield pad + f"data: {json.dumps(evt)}\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def run() -> None:
    """Entry point used by the launchd plist."""
    import uvicorn
    uvicorn.run(
        "hermes_control.main:app",
        host=config.host,
        port=config.port,
        log_level="info",
    )


if __name__ == "__main__":
    run()
