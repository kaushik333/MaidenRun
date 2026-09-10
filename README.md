# Agent Feedback Deploy

Local control plane for deploying an AI agent repo to AWS with reviewer
feedback collection built in. See [IDEA.md](../IDEA.md) for the full design
and [plan.md](../plan.md) for the phased implementation plan.

> Status: Phases 0–7 complete — repo validation, AWS credential handling,
> app EC2 provisioning, and feedback-stack EC2 provisioning (each its own
> background task + live polling, each with rollback on failure) are all
> functional via **Deploy** and the separate **Deploy feedback stack**
> action. Phase 8 (the dynamic questionnaire's JSON schema, type-based
> rendering, and Postgres storage) is not yet built — today's uploaded
> questionnaire is bundled and shipped, but the feedback-form app still
> renders its own built-in default until that phase lands. Phases 9+
> (dashboard, cost tracking, idle-timeout teardown) are not yet built.

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
cp feedback-questions.example.json feedback-questions.json
docker compose up --build
```

- Feedback form: http://localhost:8002
- Dashboard (placeholder until Phase 9): http://localhost:8003

Full architecture, directory layout, and component walkthrough will be
added in Phase 13 once the system is functionally complete.
