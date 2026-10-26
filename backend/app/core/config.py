from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # server
    backend_host: str = "0.0.0.0"
    backend_port: int = 8000
    frontend_port: int = 5173
    cors_origins: str = "http://localhost:5173"

    # logging
    log_dir: str = "logs"
    user_id: str = "local"
    log_level: str = "INFO"

    # durable, file-per-deployment history (see app/core/history.py) —
    # bind-mounted so it survives container rebuilds, not just restarts
    deployments_dir: str = "data/deployments"
    # feedback exported right before a feedback stack is torn down
    exports_dir: str = "data/exports"

    # aws
    aws_default_region: str = "us-east-1"
    aws_credentials_path: str = str(Path.home() / ".maidenrun" / "credentials.json")
    # Named AWS CLI profile to use (incl. SSO) — empty means the default
    # credential chain. Takes priority over a cached pasted key.
    aws_profile: str = ""

    # HMAC request signing (see app/core/auth.py) — empty disables auth
    # entirely, which is the default for local/dev. Set to a random value
    # (e.g. `openssl rand -hex 32`) before exposing the backend on any
    # network other than your own machine's loopback.
    control_plane_secret: str = ""

    # reserved ports — a fixed contract: the agent UI on 8001, the feedback form and dashboard on 8002/8003
    app_port: int = 8001
    feedback_form_port: int = 8002
    feedback_dashboard_port: int = 8003

    # how long the background deployment task waits for each port to
    # actually respond before giving up (EC2 boot + Docker install + image
    # build/pull)
    app_health_timeout_seconds: int = 300


settings = Settings()
