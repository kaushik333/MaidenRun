import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from app.core import history
from app.core.config import settings
from app.core.logging import logger

# Everything the control plane says to a running feedback stack goes through
# its dashboard (port 8003) with the dashboard token recorded at deploy time —
# the only route in, since Postgres has no open port. The control plane's own
# IP is always on a deployment's allowlist, so this works even when it's
# restricted.


class FeedbackStackError(Exception):
    """A feedback stack call failed; `status` is the HTTP status to report."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _dashboard(record: dict) -> tuple[str, str]:
    url = next((link["url"] for link in record.get("links", []) if link["label"] == "Feedback dashboard"), None)
    if not url:
        raise FeedbackStackError(409, "No dashboard link recorded for this deployment")
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}", parse_qs(parts.query).get("token", [""])[0]


def _call(record: dict, method: str, path: str, params: dict | None = None, body: dict | None = None) -> bytes:
    base, token = _dashboard(record)
    request = urllib.request.Request(
        f"{base}{path}?{urlencode({'token': token, **(params or {})})}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail", exc.reason)
        except ValueError:
            detail = exc.reason
        if exc.code == 404 and path.startswith("/api/admin/"):
            detail = "This feedback stack predates questionnaire versioning — redeploy it to enable this."
        raise FeedbackStackError(exc.code, str(detail)) from exc
    except urllib.error.URLError as exc:
        netloc = urlsplit(base).netloc
        raise FeedbackStackError(502, f"the feedback dashboard at {netloc} didn't respond ({exc.reason})") from exc


def export_feedback(record: dict) -> dict[str, str]:
    """Downloads every response, across every questionnaire and agent version,
    into exports_dir/<deployment_id>/ — an Excel workbook (one sheet per
    questionnaire version) plus a JSON archive. Called before teardown, since
    the data only lives on the instance's own disk. A stack deployed before
    versioning can't produce Excel, so it falls back to its CSV."""
    out_dir = Path(settings.exports_dir) / record["deployment_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    try:
        files = {"xlsx": _call(record, "GET", "/api/dashboard/export", {"format": "xlsx"})}
    except FeedbackStackError as exc:
        if exc.status != 422:  # 422 = an older stack that only knows csv/json
            raise
        files = {"csv": _call(record, "GET", "/api/dashboard/export", {"format": "csv"})}
    files["json"] = _call(record, "GET", "/api/dashboard/export", {"format": "json"})

    paths = {}
    for fmt, data in files.items():
        path = out_dir / f"feedback-{stamp}.{fmt}"
        path.write_bytes(data)
        paths[fmt] = str(path)
    logger.info("exported feedback for deployment %s to %s", record["deployment_id"], out_dir)
    return paths


def list_versions(record: dict) -> dict:
    return json.loads(_call(record, "GET", "/api/admin/questionnaires"))


def publish_questionnaire(record: dict, questionnaire_json: str, activate: bool) -> dict:
    body = {"questionnaire_json": questionnaire_json, "activate": activate}
    return json.loads(_call(record, "POST", "/api/admin/questionnaires", body=body))


def activate_questionnaire(record: dict, version: int) -> dict:
    return json.loads(_call(record, "POST", f"/api/admin/questionnaires/{version}/activate"))


def set_agent_version(record: dict, commit_sha: str, commit_message: str, repo_url: str) -> dict:
    body = {"commit_sha": commit_sha, "commit_message": commit_message, "repo_url": repo_url}
    return json.loads(_call(record, "POST", "/api/admin/agent-version", body=body))


def same_repo(a: str | None, b: str | None) -> bool:
    """https://github.com/x/y, …/y/ and …/y.git are the same repo."""
    def normalize(url: str) -> str:
        return url.strip().rstrip("/").removesuffix(".git").lower()

    return bool(a and b) and normalize(a) == normalize(b)


def announce_agent_version(repo_url: str, commit_sha: str, commit_message: str) -> None:
    """Tells every running feedback stack linked to this agent repo which
    commit is now live, so responses from here on are stamped with it.
    Best-effort: a stack that can't be reached is logged, never fails the
    agent deploy that triggered it."""
    for record in history.list_deployments():
        decisions = record.get("decisions") or {}
        if record.get("kind") != "feedback" or record.get("status") != "running":
            continue
        if not same_repo(decisions.get("agent_repo"), repo_url):
            continue
        try:
            set_agent_version(record, commit_sha, commit_message, repo_url)
            logger.info("told feedback stack %s the live agent is %s", record["deployment_id"], commit_sha)
        except FeedbackStackError:
            logger.exception("couldn't tell feedback stack %s about agent %s", record["deployment_id"], commit_sha)


def link_to_live_agent(record: dict) -> None:
    """For a feedback stack that was just deployed: announce the commit its
    linked agent repo is currently running (the newest running deployment of
    that repo), so even its very first responses are attributed."""
    agent_repo = (record.get("decisions") or {}).get("agent_repo")
    live = next(
        (
            r
            for r in history.list_deployments()  # newest first
            if r.get("kind") == "agent"
            and r.get("status") == "running"
            and same_repo((r.get("decisions") or {}).get("repo_url"), agent_repo)
            and (r.get("decisions") or {}).get("commit_sha")
        ),
        None,
    )
    if live is None:
        return
    decisions = live["decisions"]
    try:
        set_agent_version(record, decisions["commit_sha"], decisions.get("commit_message") or "", agent_repo)
    except FeedbackStackError:
        logger.exception("couldn't tell new feedback stack %s which agent is live", record["deployment_id"])
