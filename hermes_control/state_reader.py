"""Read-only access to hermes's on-disk state: cron jobs, sessions, tick lockfile.

Everything here is defensive — hermes's internal layout can change upstream.
We degrade gracefully when files/columns are missing rather than raising.
"""
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from hermes_control.config import config


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ─── Cron ────────────────────────────────────────────────────────────────────

def read_cron_jobs() -> dict[str, Any]:
    """Parse ~/.../cron/jobs.json plus last-run timestamps from cron/output/."""
    jobs_path = config.cron_jobs
    result: dict[str, Any] = {"jobs": [], "last_tick_at": None}

    # .tick.lock mtime tells us when the scheduler last ran a pass
    try:
        result["last_tick_at"] = _iso(config.cron_tick_lock.stat().st_mtime)
    except FileNotFoundError:
        pass

    if not jobs_path.exists():
        return result

    try:
        raw = json.loads(jobs_path.read_text())
    except (json.JSONDecodeError, OSError):
        return result

    # We don't know exactly how hermes shapes this JSON — it could be a list
    # or a dict keyed by job id. Handle both.
    if isinstance(raw, dict):
        entries = [{"id": k, **v} if isinstance(v, dict) else {"id": k, "value": v}
                   for k, v in raw.items()]
    elif isinstance(raw, list):
        entries = raw
    else:
        entries = []

    # Peek at cron/output/ for last-run hints
    output_mtimes: dict[str, float] = {}
    try:
        for p in config.cron_output_dir.iterdir():
            # hermes names output files like `<job-id>-<timestamp>.ndjson` or similar;
            # we just capture the most recent mtime per prefix token.
            token = p.name.split("-", 1)[0]
            mt = p.stat().st_mtime
            if mt > output_mtimes.get(token, 0):
                output_mtimes[token] = mt
    except FileNotFoundError:
        pass

    # Normalize each job into a stable shape for the UI
    normalized = []
    for j in entries:
        if not isinstance(j, dict):
            continue
        jid = str(j.get("id") or j.get("name") or "")
        last_run_at = None
        # Try explicit field, else infer from output file mtime
        if "last_run" in j and isinstance(j["last_run"], (int, float)):
            last_run_at = _iso(float(j["last_run"]))
        elif jid and any(jid.startswith(t) for t in output_mtimes):
            best = max((output_mtimes[t] for t in output_mtimes if jid.startswith(t)), default=0)
            if best:
                last_run_at = _iso(best)
        normalized.append({
            "id": jid,
            "name": j.get("name") or jid,
            "schedule": j.get("schedule") or j.get("cron") or "",
            "command": j.get("command") or j.get("prompt") or j.get("task") or "",
            "enabled": j.get("enabled", True),
            "last_run_at": last_run_at,
            "next_run_at": j.get("next_run") or None,
            "last_status": j.get("last_status") or None,
            "raw": j,  # for debugging / power users
        })
    result["jobs"] = normalized
    return result


# ─── Sessions ────────────────────────────────────────────────────────────────

def _classify_source(filename: str) -> str:
    """Infer source from filename convention."""
    if "session_cron_" in filename:
        return "cron"
    if "_chat_" in filename or filename.startswith("session_chat"):
        return "chat"
    # Generic session_YYYYMMDD_... means ad-hoc (likely TUI)
    return "interactive"


def list_sessions(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    """Enumerate session JSON files, newest first.

    We don't parse the full file here — just its mtime and a few top-level hints.
    The detail endpoint returns the full JSON if the user asks for one session.
    """
    sessions_dir = config.sessions_dir
    if not sessions_dir.is_dir():
        return []
    try:
        files = sorted(sessions_dir.glob("session_*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []

    page = files[offset:offset + limit]
    out: list[dict[str, Any]] = []
    for p in page:
        try:
            stat = p.stat()
        except OSError:
            continue
        entry: dict[str, Any] = {
            "id": p.stem,
            "source": _classify_source(p.name),
            "updated_at": _iso(stat.st_mtime),
            "size_bytes": stat.st_size,
            "title": None,
            "message_count": None,
        }
        # Peek at the top to get a title + message count without slurping the whole file
        try:
            if stat.st_size < 2_000_000:  # 2MB guard
                data = json.loads(p.read_text())
                if isinstance(data, dict):
                    entry["title"] = data.get("title") or data.get("name")
                    msgs = data.get("messages") or data.get("history") or []
                    if isinstance(msgs, list):
                        entry["message_count"] = len(msgs)
        except (json.JSONDecodeError, OSError):
            pass
        out.append(entry)
    return out


def read_session(session_id: str) -> dict[str, Any] | None:
    """Return the full JSON of one session, or None if not found."""
    sessions_dir = config.sessions_dir
    candidate = sessions_dir / f"{session_id}.json"
    if not candidate.is_file():
        return None
    try:
        return json.loads(candidate.read_text())
    except (json.JSONDecodeError, OSError):
        return None


# ─── State DB ────────────────────────────────────────────────────────────────

async def state_db_probe() -> dict[str, Any]:
    """Lightweight probe of the SQLite DB — is it openable, what tables exist."""
    db_path = config.state_db
    if not db_path.exists():
        return {"exists": False}
    # Open read-only so we can never corrupt hermes
    uri = f"file:{db_path}?mode=ro"
    try:
        async with aiosqlite.connect(uri, uri=True) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ) as cur:
                tables = [r[0] async for r in cur]
        return {"exists": True, "tables": tables, "size_bytes": db_path.stat().st_size}
    except Exception as e:  # intentionally broad — any DB-side error is non-fatal
        return {"exists": True, "error": str(e), "size_bytes": db_path.stat().st_size}


# ─── Config peek ─────────────────────────────────────────────────────────────

def config_summary() -> dict[str, Any]:
    """Very small peek at config.yaml for display — never dumps full contents
    (may contain secrets despite the separate .env)."""
    p = config.config_yaml
    if not p.exists():
        return {"exists": False}
    try:
        stat = p.stat()
        return {
            "exists": True,
            "size_bytes": stat.st_size,
            "updated_at": _iso(stat.st_mtime),
        }
    except OSError:
        return {"exists": True}


# ─── Uptime helper ───────────────────────────────────────────────────────────

_started_at = time.monotonic()


def sidecar_uptime_s() -> int:
    return int(time.monotonic() - _started_at)
