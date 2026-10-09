# Adapt this repo for deployment

Hand this file to your own coding agent (Claude Code, Cursor, etc.),
alongside this repo, to restructure it so it fits the deploy platform's
entry-point contract. **The platform never runs this itself or any coding
agent on your behalf — this is a prompt for your own agent, in your own
environment, at your own risk.** Review every change before deploying, the
same way you'd review any AI-generated diff.

## The contract to satisfy

1. **Single entry point at the repo root**: either one compose file
   (`compose.yaml` or `docker-compose.yml`), or, if there's none, a single
   `Dockerfile`. The platform runs exactly one command against this repo
   (`docker compose up`, or an equivalent single-container build/run) —
   nothing smarter than that.
2. **No multi-file compose chaining.** `-f` chaining,
   `docker-compose.override.yml`, and similar setups are not supported.
   Consolidate every service the code needs — the agent itself, plus any
   local DB, vector store, or cache — into that one root compose file.
3. **The agent UI publishes on host port 8001.** No other service may map a
   host port to 8002 or 8003 — those are reserved by the platform for the
   feedback form and dashboard.

## What's out of scope

Only restructure for the entry-point contract above (compose file location,
service consolidation, port 8001). Do not:

- Change what the agent itself does, its business logic, or its prompts.
- Add, remove, or reconfigure external services (Pinecone, RDS, Langfuse,
  etc.) — those are consumed via environment variables the platform injects
  at deploy time, not declared in the compose file.
- Declare those environment variables in the compose file. The platform
  injects every variable entered on the deploy form into every service, and
  those values override same-named ones in the compose file.
- Rename or restructure anything not required to satisfy the contract
  above.

## Suggested steps

1. Check current compatibility first — see `compatibility.md` — so you know
   exactly which of the 3 contract points already pass.
2. If multiple compose files exist, merge them into a single `compose.yaml`
   (or `docker-compose.yml`) at the repo root and delete the others.
3. If the service meant to be reviewed doesn't map to host port 8001,
   update its `ports:` entry to do so.
4. If there's no compose file and no `Dockerfile` at the root,
   add whichever fits the project best.
5. Re-run the compatibility check to confirm all points now pass.
