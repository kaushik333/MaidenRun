from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # server
    feedback_form_port: int = 8002
    cors_origins: str = "*"

    # Same image, two containers (see feedback-stack/docker-compose.yml) —
    # this decides whether a given process serves the public questionnaire
    # form or the token-gated dashboard, since they can't both own `/`.
    service_role: Literal["form", "dashboard"] = "form"

    # Checked against ?token=... on every dashboard request. Fetched from
    # SSM by the EC2 boot script (see backend/app/aws/feedback_stack.py in
    # the control plane) — deliberately has no default that would ever
    # grant access; an unset token means the dashboard is unreachable
    # rather than silently open.
    dashboard_token: str = ""

    # postgres — always passed in by feedback-stack/docker-compose.yml,
    # pointing at the postgres service on the compose network
    database_url: str = "postgresql://postgres:postgres@localhost:5432/feedback"

    # tags every submitted row; set by the EC2 boot script per deployment
    deployment_id: str = "local"

    # bundled in at the root of the feedback-stack S3 bundle, alongside
    # docker-compose.yml — see questions.py and ../../backend/app/aws/s3_bundle.py
    feedback_questions_path: str = "/opt/feedback-stack/feedback-questions.json"

    log_level: str = "INFO"


settings = Settings()
