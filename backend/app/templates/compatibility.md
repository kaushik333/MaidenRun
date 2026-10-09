# Compatibility check

Hand this file to your own coding agent (or read it yourself) to check
whether this repo already fits the deploy platform's entry-point contract —
**without changing anything**. This is read-only: it only inspects the repo
and reports back. If you want the repo actually restructured to fix any
gaps found here, use `adapt.md` instead.

## The contract

1. The repo has a **single entry point at its root**: either one
   compose file (`compose.yaml` or `docker-compose.yml`), or (if there's
   none) a single `Dockerfile`.
2. Multi-file compose setups (`-f` chaining, `docker-compose.override.yml`,
   etc.) are **not supported** — everything must be consolidated into that
   one root compose file.
3. Whichever service is meant to be reviewed publishes on **host port
   8001**.
4. No service maps a host port to **8002** or **8003** — those are reserved
   by the platform for the feedback form and dashboard.

A bare `Dockerfile` (no compose file) always satisfies 3 and 4 trivially:
with no compose file, the platform decides all host-side port bindings
itself, so there's nothing to clash with. An `EXPOSE` line in a Dockerfile
is documentation only — it never creates a real clash.

## What to report

Walk the repo root and report exactly one line per check below, each
prefixed with ✔ (pass) or ✘ (fail):

```
✔ Single compose file found at repo root (compose.yaml)
✘ No service maps to host port 8001 (agent UI not exposed)
✘ A service maps to host port 8002 — conflicts with reserved feedback port
✔ No multi-file compose chaining detected
```

If the repo has no compose file at all, check `Dockerfile` instead
and adjust the first line's wording accordingly (e.g. "Single Dockerfile
found at repo root").

Do not edit any files. Do not run `docker build` or `docker compose up`.
This is a static, read-only inspection.

## Automated version

The platform can also generate this same checklist for you automatically:
`GET /compatibility-report?repo=<your-repo-url>`.
