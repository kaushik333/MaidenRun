import subprocess
import tempfile
from pathlib import Path

import yaml

from app.validation.models import ValidateRepoResponse

APP_PORT = 8001
RESERVED_FEEDBACK_PORTS = {8002: "feedback capture", 8003: "the feedback dashboard"}
CLONE_TIMEOUT_SECONDS = 300


def _authenticated_url(repo_url: str, github_token: str | None) -> str:
    """Inserts a GitHub personal access token into the URL the way GitHub's
    own HTTPS auth expects it (`https://<token>@github.com/...`). Only ever
    used for the actual `git` subprocess call — every error path below
    reports the original, unauthenticated repo_url instead."""
    if not github_token:
        return repo_url
    scheme, sep, rest = repo_url.partition("://")
    return f"{scheme}{sep}{github_token}@{rest}" if sep else repo_url


def clone_repo(repo_url: str, dest: Path, github_token: str | None = None) -> str | None:
    """Shallow-clones repo_url into dest. Returns an error message, or None on success."""
    clone_url = _authenticated_url(repo_url, github_token)
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", clone_url, str(dest)],
            capture_output=True,
            text=True,
            timeout=CLONE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"Cloning {repo_url} took too long (over {CLONE_TIMEOUT_SECONDS}s) and was aborted."
    except OSError as exc:
        return f"Could not run git to clone {repo_url}: {exc}"

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if github_token:
            # git itself sometimes echoes the URL it was given back in its
            # own error text (e.g. "repository 'https://TOKEN@...' not
            # found") — scrub it out even though we never embed it ourselves.
            stderr = stderr.replace(github_token, "***")
        return f"Could not clone {repo_url}: {stderr}"
    return None


def host_ports_of_service(service: dict) -> list[int]:
    ports = []
    for entry in service.get("ports", []) or []:
        if isinstance(entry, str):
            # "8001:3000", "8001:3000/tcp", "127.0.0.1:8001:3000" map a host port;
            # a bare "3000" has no explicit host-side mapping and can't clash.
            left = entry.split("/")[0]
            parts = left.split(":")
            if len(parts) >= 2:
                try:
                    ports.append(int(parts[-2]))
                except ValueError:
                    pass
        elif isinstance(entry, dict) and "published" in entry:
            try:
                ports.append(int(entry["published"]))
            except (TypeError, ValueError):
                pass
    return ports


def _validate_compose(compose_path: Path) -> ValidateRepoResponse:
    try:
        parsed = yaml.safe_load(compose_path.read_text()) or {}
    except yaml.YAMLError as exc:
        return ValidateRepoResponse(valid=False, error=f"Could not parse {compose_path.name}: {exc}")

    services = parsed.get("services") or {}
    host_ports: dict[int, str] = {}
    for name, service in services.items():
        for port in host_ports_of_service(service or {}):
            host_ports.setdefault(port, name)

    for port, purpose in RESERVED_FEEDBACK_PORTS.items():
        if port in host_ports:
            return ValidateRepoResponse(
                valid=False,
                entry_point="compose",
                error=(
                    f"Port {port} is reserved by the platform for {purpose}. Update "
                    "docker-compose.yml to use a different host port for that service "
                    "and redeploy."
                ),
            )

    if APP_PORT not in host_ports:
        return ValidateRepoResponse(
            valid=False,
            entry_point="compose",
            error=(
                f"No service maps a host port to {APP_PORT}. The platform expects your "
                "agent UI on host port 8001 — update docker-compose.yml."
            ),
        )

    return ValidateRepoResponse(valid=True, entry_point="compose")


def validate_repo_dir(repo_dir: Path) -> ValidateRepoResponse:
    compose_files = sorted(repo_dir.glob("docker-compose*.y*ml"))
    dockerfile = repo_dir / "Dockerfile"

    if len(compose_files) > 1:
        names = ", ".join(f.name for f in compose_files)
        return ValidateRepoResponse(
            valid=False,
            error=(
                f"Multiple docker-compose files found ({names}). Multi-file compose "
                "setups (override/-f chaining) are not supported — consolidate "
                "everything into a single root docker-compose.yml."
            ),
        )

    if compose_files:
        return _validate_compose(compose_files[0])

    if dockerfile.is_file():
        return ValidateRepoResponse(valid=True, entry_point="dockerfile")

    return ValidateRepoResponse(
        valid=False,
        error="No entry point found. Add a docker-compose.yml or Dockerfile at the repo root.",
    )


def validate_repo(repo_url: str, github_token: str | None = None) -> ValidateRepoResponse:
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "repo"
        clone_error = clone_repo(repo_url, dest, github_token)
        if clone_error:
            return ValidateRepoResponse(valid=False, error=clone_error)
        return validate_repo_dir(dest)
