import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from app.core.config import settings
from app.core.logging import logger


def export_feedback(record: dict) -> dict[str, str]:
    """Downloads every feedback submission from a running feedback stack's
    dashboard (CSV + JSON) into exports_dir/<deployment_id>/, returning
    {"csv": path, "json": path}. Called before teardown, since the data
    only lives on the instance's own disk. Raises OSError/ValueError if the
    stack can't be reached — the caller decides whether to proceed anyway."""
    dashboard_url = next(
        (link["url"] for link in record.get("links", []) if link["label"] == "Feedback dashboard"), None
    )
    if not dashboard_url:
        raise ValueError("no dashboard link recorded for this deployment")
    parts = urlsplit(dashboard_url)
    token = parse_qs(parts.query).get("token", [""])[0]

    out_dir = Path(settings.exports_dir) / record["deployment_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    paths = {}
    for fmt in ("csv", "json"):
        url = f"{parts.scheme}://{parts.netloc}/api/dashboard/export?{urlencode({'token': token, 'format': fmt})}"
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                data = response.read()
        except urllib.error.URLError as exc:
            raise OSError(f"the feedback dashboard at {parts.netloc} didn't respond ({exc.reason})") from exc
        path = out_dir / f"feedback-{stamp}.{fmt}"
        path.write_bytes(data)
        paths[fmt] = str(path)
    logger.info("exported feedback for deployment %s to %s", record["deployment_id"], out_dir)
    return paths
