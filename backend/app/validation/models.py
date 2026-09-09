from pydantic import BaseModel, Field


class ValidateRepoRequest(BaseModel):
    repo_url: str = Field(min_length=1)


class ValidateRepoResponse(BaseModel):
    valid: bool
    entry_point: str | None = None
    error: str | None = None


class CheckResult(BaseModel):
    ok: bool
    text: str


class CompatibilityReport(BaseModel):
    checks: list[CheckResult]
    rendered: str
