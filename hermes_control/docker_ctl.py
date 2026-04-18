"""Thin async wrapper around the `docker` CLI.

Sidecar is deliberately shell-based rather than using docker-py: fewer deps,
survives Docker SDK churn, and matches how a human would operate this.
"""
import asyncio
import json
import shutil
from typing import Any

# OrbStack installs docker at /usr/local/bin/docker; pick that up explicitly so
# we don't depend on whatever PATH launchd hands us.
_DOCKER = shutil.which("docker") or "/usr/local/bin/docker"


async def _run(args: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    """Run `docker <args...>`; returns (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        _DOCKER, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"docker {' '.join(args)} timed out after {timeout}s"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def inspect(name: str) -> dict[str, Any] | None:
    """Return the raw `docker inspect` JSON for a container, or None if missing."""
    rc, out, _ = await _run(["inspect", name])
    if rc != 0:
        return None
    try:
        arr = json.loads(out)
        return arr[0] if arr else None
    except (json.JSONDecodeError, IndexError):
        return None


async def summary(name: str) -> dict[str, Any]:
    """Compact status dict for the /status endpoint."""
    info = await inspect(name)
    if info is None:
        return {"exists": False, "running": False}
    state = info.get("State", {}) or {}
    cfg = info.get("Config", {}) or {}
    host_cfg = info.get("HostConfig", {}) or {}
    return {
        "exists": True,
        "running": state.get("Running", False),
        "paused": state.get("Paused", False),
        "restarting": state.get("Restarting", False),
        "status": state.get("Status", "unknown"),
        "started_at": state.get("StartedAt"),
        "restart_count": info.get("RestartCount", 0),
        "restart_policy": (host_cfg.get("RestartPolicy") or {}).get("Name", ""),
        "image": cfg.get("Image", ""),
        "pid": state.get("Pid") or None,
    }


async def logs(name: str, tail: int = 500, since: str | None = None) -> list[str]:
    """Grab recent container logs. Docker merges stdout+stderr when both requested."""
    args = ["logs", "--tail", str(tail)]
    if since:
        args += ["--since", since]
    args.append(name)
    rc, out, err = await _run(args, timeout=15.0)
    if rc != 0:
        return [f"[hermes-control] docker logs failed rc={rc}: {err.strip()}"]
    # `docker logs` writes app output to both stdout and stderr; combine
    combined = (out + err).splitlines()
    return combined


async def pause(name: str) -> dict[str, Any]:
    rc, out, err = await _run(["pause", name])
    return {"ok": rc == 0, "rc": rc, "stdout": out.strip(), "stderr": err.strip()}


async def unpause(name: str) -> dict[str, Any]:
    rc, out, err = await _run(["unpause", name])
    return {"ok": rc == 0, "rc": rc, "stdout": out.strip(), "stderr": err.strip()}


async def restart(name: str, timeout_s: int = 30) -> dict[str, Any]:
    rc, out, err = await _run(["restart", "-t", str(timeout_s), name], timeout=timeout_s + 10)
    return {"ok": rc == 0, "rc": rc, "stdout": out.strip(), "stderr": err.strip()}


async def list_orphans(parent_name: str) -> list[dict[str, Any]]:
    """Find compose-run leftovers like `hermes-agent-hermes-agent-run-*`."""
    filter_expr = f"name={parent_name}-{parent_name}-run-"
    rc, out, _ = await _run([
        "ps", "-a",
        "--filter", filter_expr,
        "--format", "{{.Names}}|{{.Status}}|{{.State}}|{{.ID}}",
    ])
    if rc != 0:
        return []
    result = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) >= 4:
            result.append({"name": parts[0], "status": parts[1], "state": parts[2], "id": parts[3]})
    return result


async def prune_orphans(parent_name: str) -> dict[str, Any]:
    """Remove every `<parent>-<parent>-run-*` container.

    `docker rm -f` handles both running and stopped ones, so this forcibly
    reclaims leaked compose-run containers. We never touch the main container
    because its name doesn't match this prefix.
    """
    orphans = await list_orphans(parent_name)
    if not orphans:
        return {"removed": [], "skipped": 0}
    ids = [o["id"] for o in orphans]
    rc, out, err = await _run(["rm", "-f", *ids], timeout=30.0)
    return {
        "removed": [o["name"] for o in orphans] if rc == 0 else [],
        "skipped": 0 if rc == 0 else len(orphans),
        "rc": rc,
        "stderr": err.strip(),
    }
