import tempfile
from pathlib import Path

import yaml

from app.validation.models import CheckResult, CompatibilityReport
from app.validation.validator import (
    APP_PORT,
    RESERVED_FEEDBACK_PORTS,
    clone_repo,
    find_compose_files,
    host_ports_of_service,
)

RESERVED_PORT_LABELS = {8002: "feedback", 8003: "feedback dashboard"}


def render_checklist(checks: list[CheckResult]) -> str:
    return "\n".join(f"{'✔' if c.ok else '✘'} {c.text}" for c in checks)


def compatibility_checklist(repo_dir: Path) -> list[CheckResult]:
    compose_files = find_compose_files(repo_dir)
    dockerfile = repo_dir / "Dockerfile"
    checks: list[CheckResult] = []

    entry_point: str | None = None
    if len(compose_files) > 1:
        names = ", ".join(f.name for f in compose_files)
        checks.append(
            CheckResult(
                ok=False,
                text=f"Single compose file found at repo root (found {len(compose_files)}: {names})",
            )
        )
    elif compose_files:
        entry_point = "compose"
        checks.append(CheckResult(ok=True, text=f"Single compose file found at repo root ({compose_files[0].name})"))
    elif dockerfile.is_file():
        entry_point = "dockerfile"
        checks.append(CheckResult(ok=True, text="Single Dockerfile found at repo root"))
    else:
        checks.append(CheckResult(ok=False, text="No compose file (compose.yaml / docker-compose.yml) or Dockerfile found at repo root"))

    host_ports: dict[int, str] = {}
    parse_error: str | None = None
    if entry_point == "compose":
        try:
            parsed = yaml.safe_load(compose_files[0].read_text()) or {}
        except yaml.YAMLError as exc:
            parse_error = str(exc)
        else:
            for name, service in (parsed.get("services") or {}).items():
                for port in host_ports_of_service(service or {}):
                    host_ports.setdefault(port, name)

    if entry_point == "dockerfile":
        checks.append(
            CheckResult(ok=True, text="Host port 8001 will be bound by the platform (Dockerfile-only entry point)")
        )
    elif entry_point == "compose" and parse_error is None:
        if APP_PORT in host_ports:
            checks.append(CheckResult(ok=True, text="A service maps to host port 8001 (agent UI exposed)"))
        else:
            checks.append(CheckResult(ok=False, text="No service maps to host port 8001 (agent UI not exposed)"))
    else:
        checks.append(
            CheckResult(ok=False, text="Could not confirm host port 8001 — resolve the entry point issue above first")
        )

    if entry_point == "dockerfile":
        checks.append(CheckResult(ok=True, text="No reserved-port conflict possible (Dockerfile-only entry point)"))
    elif entry_point == "compose" and parse_error is None:
        clashed = [port for port in RESERVED_FEEDBACK_PORTS if port in host_ports]
        if clashed:
            port = clashed[0]
            checks.append(
                CheckResult(
                    ok=False,
                    text=(
                        f"A service maps to host port {port} — conflicts with reserved "
                        f"{RESERVED_PORT_LABELS[port]} port"
                    ),
                )
            )
        else:
            checks.append(CheckResult(ok=True, text="No service maps to reserved ports 8002/8003"))
    else:
        checks.append(
            CheckResult(
                ok=False,
                text="Could not confirm reserved ports 8002/8003 — resolve the entry point issue above first",
            )
        )

    if len(compose_files) > 1:
        checks.append(CheckResult(ok=False, text="Multi-file compose setup detected — not supported in v1"))
    else:
        checks.append(CheckResult(ok=True, text="No multi-file compose chaining detected"))

    return checks


def compatibility_report(repo_url: str) -> CompatibilityReport:
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "repo"
        clone_error = clone_repo(repo_url, dest)
        if clone_error:
            checks = [CheckResult(ok=False, text=clone_error)]
            return CompatibilityReport(checks=checks, rendered=render_checklist(checks))
        checks = compatibility_checklist(dest)
        return CompatibilityReport(checks=checks, rendered=render_checklist(checks))
