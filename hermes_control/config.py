"""Sidecar configuration — paths, container name, auth token."""
import os
from pathlib import Path


class Config:
    """Resolve defaults from env vars, fall back to stock layout on Mac Mini."""

    def __init__(self) -> None:
        self.container_name = os.environ.get("HERMES_CONTAINER_NAME", "hermes-agent")
        self.data_dir = Path(os.environ.get(
            "HERMES_DATA_DIR",
            str(Path.home() / "docker-projects" / "hermes-agent" / "data"),
        ))
        self.token_path = Path(os.environ.get(
            "HERMES_CONTROL_TOKEN_PATH",
            str(Path.home() / ".config" / "hermes-control" / "token"),
        ))
        self.host = os.environ.get("HERMES_CONTROL_HOST", "0.0.0.0")
        self.port = int(os.environ.get("HERMES_CONTROL_PORT", "7878"))
        # Log lookback cap — protects us from a user asking for 1M lines over the network
        self.max_log_tail = int(os.environ.get("HERMES_CONTROL_MAX_LOG_TAIL", "5000"))

    @property
    def state_db(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def cron_jobs(self) -> Path:
        return self.data_dir / "cron" / "jobs.json"

    @property
    def cron_tick_lock(self) -> Path:
        return self.data_dir / "cron" / ".tick.lock"

    @property
    def cron_output_dir(self) -> Path:
        return self.data_dir / "cron" / "output"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def config_yaml(self) -> Path:
        return self.data_dir / "config.yaml"

    def load_token(self) -> str | None:
        """Read the bearer token. Returns None if missing; sidecar refuses to start."""
        try:
            return self.token_path.read_text().strip() or None
        except FileNotFoundError:
            return None


config = Config()
