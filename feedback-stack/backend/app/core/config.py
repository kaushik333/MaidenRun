from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # server
    feedback_form_port: int = 8002
    cors_origins: str = "*"

    # postgres — always passed in by feedback-stack/docker-compose.yml,
    # pointing at the postgres service on the compose network
    database_url: str = "postgresql://postgres:postgres@localhost:5432/feedback"

    # tags every submitted row; set by the EC2 boot script per deployment
    deployment_id: str = "local"

    # written by the EC2 boot script from the deployed agent repo's own
    # feedback-questions.yaml, if it had one — see questions.py
    feedback_questions_path: str = "/opt/feedback-questions.yaml"

    log_level: str = "INFO"


settings = Settings()
