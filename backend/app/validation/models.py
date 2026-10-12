from pydantic import BaseModel, Field


class ValidateRepoRequest(BaseModel):
    repo_url: str = Field(min_length=1)
    github_token: str | None = None


class ValidateRepoResponse(BaseModel):
    valid: bool
    entry_point: str | None = None
    error: str | None = None
    # The exact commit that was validated — the instance is pinned to it, and
    # feedback is recorded against it.
    commit_sha: str | None = None
    commit_message: str | None = None


class CheckResult(BaseModel):
    ok: bool
    text: str


class CompatibilityReport(BaseModel):
    checks: list[CheckResult]
    rendered: str
