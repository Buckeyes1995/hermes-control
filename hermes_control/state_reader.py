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

    # Hermes v0.4+ shape: { "jobs": [...] }. Older shapes might be a bare list
    # or a dict keyed by id — handle all three.
    entries: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        if isinstance(raw.get("jobs"), list):
            entries = [j for j in raw["jobs"] if isinstance(j, dict)]
        else:
            entries = [{"id": k, **v} for k, v in raw.items() if isinstance(v, dict)]
    elif isinstance(raw, list):
        entries = [j for j in raw if isinstance(j, dict)]

    # Normalize each job into a stable shape for the UI
    normalized = []
    for j in entries:
        jid = str(j.get("id") or j.get("name") or "")
        # schedule can be a nested {kind,expr,display} or a flat string
        schedule = j.get("schedule")
        if isinstance(schedule, dict):
            schedule_str = schedule.get("display") or schedule.get("expr") or ""
        else:
            schedule_str = str(schedule or j.get("cron") or "")
        normalized.append({
            "id": jid,
            "name": j.get("name") or jid,
            "schedule": schedule_str,
            "command": j.get("prompt") or j.get("command") or j.get("task") or "",
            "script": j.get("script"),
            "enabled": j.get("enabled", True),
            "state": j.get("state"),
            "last_run_at": j.get("last_run_at"),
            "next_run_at": j.get("next_run_at"),
            "last_status": j.get("last_status"),
            "last_error": j.get("last_error"),
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
