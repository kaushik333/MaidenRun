# Agent Feedback Deploy

Local control plane for deploying an AI agent repo to AWS with reviewer
feedback collection built in. See [IDEA.md](../IDEA.md) for the full design
and [plan.md](../plan.md) for the phased implementation plan.

> Status: Phases 0–6 complete — repo validation, AWS credential handling,
> and app EC2 provisioning (background task + live polling, rollback on
> failure) are all functional via **Deploy**. Phases 7–8 (a separate
> **Deploy feedback stack** action, plus the dynamic questionnaire) are
> partway done: the feedback-form app itself works standalone (see below)
> but isn't yet wired up to a live AWS deploy path. Phases 9+ (dashboard,
> cost tracking, idle-timeout teardown) are not yet built.

## Local dev

```bash
cp .env.example .env
docker compose -f docker-compose.dev.yml up
```

- Backend: http://localhost:8000/health
- Frontend: http://localhost:5173

### Without Docker

```bash
# backend
cd backend
uv venv && uv pip install -e ".[dev]"
uv run uvicorn app.main:app --reload

# frontend
cd frontend
npm install
npm run dev
```

### Feedback stack (`feedback-stack/`)

The questionnaire form + dashboard that get deployed onto the user's own
feedback-stack EC2 instance are a separate app with their own Docker setup —
see [`feedback-stack/`](feedback-stack/). To run it standalone:

```bash
cd feedback-stack
cp .env.example .env
docker compose up --build
```

- Feedback form: http://localhost:8002
- Dashboard (placeholder until Phase 9): http://localhost:8003

Full architecture, directory layout, and component walkthrough will be
added in Phase 13 once the system is functionally complete.
