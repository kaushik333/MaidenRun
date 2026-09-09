from fastapi import APIRouter, Query

from app.validation.compatibility import compatibility_report
from app.validation.models import CompatibilityReport

router = APIRouter()


@router.get("/compatibility-report", response_model=CompatibilityReport)
def compatibility_report_endpoint(repo: str = Query(..., min_length=1)) -> CompatibilityReport:
    return compatibility_report(repo)
