"""One-shot chat to hermes via `docker exec hermes-agent hermes chat -q ...`.

Hermes's `chat -q` subcommand is non-interactive: one query in, one response
out, plus a trailing `session_id: <id>` line so we can thread follow-up turns
with `--resume <id>`. We wrap that in an async generator that yields SSE-style
events as the subprocess streams stdout.
"""
import asyncio
import re
import shutil
from dataclasses import dataclass, field
from typing import AsyncIterator

from hermes_control.config import config

_DOCKER = shutil.which("docker") or "/usr/local/bin/docker"
_SESSION_RE = re.compile(r"^session_id:\s*(\S+)\s*$")


@dataclass
class ChatRequest:
    prompt: str
    session_id: str | None = None     # if set, --resume <id>
    max_turns: int = 30
    skills: list[str] = field(default_factory=list)
    source: str = "crucible"


async def run_chat(req: ChatRequest) -> AsyncIterator[dict]:
    """Yield SSE-ish event dicts as hermes chat streams output.

    Events:
      { "event": "start" }
      { "event": "line", "line": "<stdout line>" }
      { "event": "done", "session_id": "<sid>" | None, "exit_code": int }
      { "event": "error", "message": "..." }
    """
    args = [
        "exec", "-i", config.container_name,
        "hermes", "chat", "-q", req.prompt,
        "--quiet",
        "--source", req.source,
        "--max-turns", str(req.max_turns),
    ]
    if req.session_id:
        args += ["--resume", req.session_id]
    if req.skills:
        args += ["-s", ",".join(req.skills)]

    yield {"event": "start"}

    try:
        proc = await asyncio.create_subprocess_exec(
            _DOCKER, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        yield {"event": "error", "message": "docker binary not found"}
        return
    except Exception as e:
        yield {"event": "error", "message": f"failed to spawn docker: {e}"}
        return

    session_id: str | None = None

    async def _pump(stream, is_err: bool) -> AsyncIterator[dict]:
        nonlocal session_id
        if stream is None:
            return
        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            # Hermes writes `session_id: <id>` as the last stdout line — capture
            m = _SESSION_RE.match(line)
            if m and not is_err:
                session_id = m.group(1)
                continue  # don't echo the session-id noise back to the client
            # Skip the decorative banner lines from `╭─ ⚕ Hermes ───...`
            if line.startswith("╭") or line.startswith("╰") or line.strip() == "":
                if line.strip() == "":
                    yield {"event": "line", "line": ""}  # preserve blank lines inside responses
                continue
            yield {"event": "line", "line": line}

    # Merge stdout+stderr concurrently so error output shows up live too
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def _drain(gen):
        async for item in gen:
            await queue.put(item)
        await queue.put(None)

    t_out = asyncio.create_task(_drain(_pump(proc.stdout, False)))
    t_err = asyncio.create_task(_drain(_pump(proc.stderr, True)))

    finished_streams = 0
    while finished_streams < 2:
        item = await queue.get()
        if item is None:
            finished_streams += 1
            continue
        yield item

    rc = await proc.wait()
    await asyncio.gather(t_out, t_err, return_exceptions=True)
    yield {"event": "done", "session_id": session_id, "exit_code": rc}
