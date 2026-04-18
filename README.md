# hermes-control

HTTP control sidecar for a Dockerized [hermes-agent](https://github.com/NousResearch/hermes-agent). Gives [Crucible](https://github.com/Buckeyes1995/crucible) (or any other client) a clean HTTP surface to see what hermes is doing, tail its logs, inspect sessions and cron jobs, and drive the container lifecycle (pause / resume / restart).

Runs natively on the same host as the hermes container. Reads hermes's bind-mounted state directory read-only. Uses the local `docker` CLI for lifecycle operations. Does not require any modifications to hermes upstream.

## Architecture

```
Crucible  ──HTTP/bearer──►  hermes-control (FastAPI :7878)
                                    │
                           reads ~/docker-projects/hermes-agent/data/
                           runs   docker pause|unpause|restart|logs hermes-agent
                                    │
                                    ▼
                           hermes-agent container (OrbStack)
```

## Install

```bash
# 1. Clone
git clone https://github.com/Buckeyes1995/hermes-control ~/projects/hermes-control
cd ~/projects/hermes-control

# 2. venv
/opt/homebrew/bin/python3.13 -m venv ~/.venvs/hermes-control
~/.venvs/hermes-control/bin/pip install -e .

# 3. Bearer token
mkdir -p ~/.config/hermes-control
openssl rand -hex 32 > ~/.config/hermes-control/token
chmod 600 ~/.config/hermes-control/token

# 4. launchd
cp packaging/com.jim.hermes-control.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.jim.hermes-control.plist

# 5. Verify
curl -H "Authorization: Bearer $(cat ~/.config/hermes-control/token)" \
     http://localhost:7878/status | python3 -m json.tool
```

## Endpoints

All except `/health` require `Authorization: Bearer <token>`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness probe (no auth) |
| GET | `/status` | Full dashboard payload — container + cron + sessions + orphans |
| GET | `/sessions?limit=N&offset=N` | Recent sessions |
| GET | `/sessions/{id}` | One full session JSON |
| GET | `/cron` | Cron jobs + last tick time |
| GET | `/logs?tail=N&since=10m` | Container log tail via `docker logs` |
| POST | `/pause` | `docker pause hermes-agent` |
| POST | `/resume` | `docker unpause hermes-agent` |
| POST | `/restart` | `docker restart hermes-agent` |
| GET | `/orphans` | List `hermes-agent-*-run-*` containers |
| POST | `/orphans/prune` | `docker rm -f` the orphans |

## Configuration

Env vars (all optional — defaults match a stock install):

| Var | Default |
|---|---|
| `HERMES_CONTAINER_NAME` | `hermes-agent` |
| `HERMES_DATA_DIR` | `~/docker-projects/hermes-agent/data` |
| `HERMES_CONTROL_TOKEN_PATH` | `~/.config/hermes-control/token` |
| `HERMES_CONTROL_HOST` | `0.0.0.0` |
| `HERMES_CONTROL_PORT` | `7878` |
| `HERMES_CONTROL_MAX_LOG_TAIL` | `5000` |

## Safety

- State DB is opened with `mode=ro` — the sidecar can never write to it.
- Config file peek returns only size + mtime, never contents.
- `/orphans/prune` matches only `<container>-<container>-run-*` names; the main container is never in that set.
- Bearer token is required for everything except `/health`.

## Logs

`~/Library/Logs/hermes-control.log`

```bash
tail -f ~/Library/Logs/hermes-control.log
```

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.jim.hermes-control.plist
rm ~/Library/LaunchAgents/com.jim.hermes-control.plist
rm -rf ~/.venvs/hermes-control ~/.config/hermes-control ~/projects/hermes-control
```
