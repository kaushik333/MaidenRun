# Agent Feedback Deploy

Local control plane for deploying an AI agent repo to AWS with reviewer
feedback collection built in. See [IDEA.md](../IDEA.md) for the full design
and [plan.md](../plan.md) for the phased implementation plan.

> Status: Phase 0 (scaffolding) — not yet functional.

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

Full architecture, directory layout, and component walkthrough will be
added in Phase 12 once the system is functionally complete.
