from fastapi import APIRouter

from app.validation.models import ValidateRepoRequest, ValidateRepoResponse
from app.validation.validator import validate_repo

router = APIRouter()


@router.post("/validate-repo", response_model=ValidateRepoResponse)
def validate_repo_endpoint(request: ValidateRepoRequest) -> ValidateRepoResponse:
    return validate_repo(request.repo_url)
