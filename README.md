# AgentDock

Local control plane for deploying an AI agent repo to AWS with reviewer
feedback collection built in. See [IDEA.md](../IDEA.md) for the full design
and [plan.md](../plan.md) for the phased implementation plan.

> Status: Phases 0–10 complete — repo validation, AWS credential handling,
> app EC2 provisioning, feedback-stack EC2 provisioning, the dynamic
> questionnaire, the token-gated feedback dashboard (`:8003`), and a live
> AWS cost-estimate widget (EC2+EBS for each instance, plus S3 for the
> feedback-stack's bundle-relay bucket) are all functional via **Deploy**
> and the separate **Deploy feedback stack** action. Phase 11+
> (idle-timeout teardown, security hardening) is not yet built.

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
- Dashboard: http://localhost:8003/?token=local-dev-token (matches `DASHBOARD_TOKEN` in `.env.example`)

Full architecture, directory layout, and component walkthrough will be
added in Phase 13 once the system is functionally complete.
