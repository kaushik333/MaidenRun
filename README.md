<div align="center">

<img src="docs/images/logo.svg" alt="MaidenRun logo" width="88" />

# MaidenRun

**Get your AI agent in front of real testers, and find out what they think.**

Point MaidenRun at your agent's GitHub repo. It deploys the agent to your own AWS account,<br/>
gives you a link to share, and collects ratings and comments on a live feedback dashboard.

[![CI](https://github.com/kaushik333/MaidenRun/actions/workflows/ci.yml/badge.svg)](https://github.com/kaushik333/MaidenRun/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-7c3aed.svg)](LICENSE)
![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/docker-compose-2496ED?logo=docker&logoColor=white)
![AWS](https://img.shields.io/badge/AWS-EC2-FF9900?logo=amazonwebservices&logoColor=white)

<br/>

<img src="docs/images/dashboard.png" alt="The MaidenRun feedback dashboard" width="900" />

</div>

## Why MaidenRun

Tracing tools like LangSmith and Langfuse show you what your agent *did*. MaidenRun tells you what people *thought of it*. That's the question that matters before launch, and it usually means hacking together hosting, a form, and a spreadsheet.

- **One link to share.** Your agent runs on a small EC2 instance at a fixed address, even when you redeploy.
- **Feedback built in.** A form for testers and a dashboard for you, with Excel and JSON export.
- **Compare versions.** Push a fix, click **Redeploy**, and see whether ratings went up for the new commit.
- **Your account, your data.** Everything runs in your AWS account. Secrets go to AWS Parameter Store, never into the instance's boot script.
- **Nothing left behind.** Teardown deletes every resource a deployment created, and a sweep finds anything left over in any region.

## Quickstart

You need [Docker](https://docs.docker.com/get-docker/) and an AWS account.

```bash
git clone https://github.com/kaushik333/MaidenRun.git
cd MaidenRun
cp .env.example .env
docker compose up --build
```

Open **http://localhost:5173**.

Then, once per AWS account, create a dedicated IAM user for MaidenRun. It takes about five minutes: follow [infra/README.md](infra/README.md), or click **AWS one-time setup** in the app.

## How to use it

1. **Deploy your agent.** Paste the repo link, click **Check**, add your LLM API key, and click **Deploy**. You get a link to the agent in a few minutes.
2. **Deploy a feedback stack.** On the **Feedback stack** tab, upload a questionnaire ([example](feedback-stack/feedback-questions.example.json)) and pick the agent it collects feedback for. Share the form link with testers. The dashboard link is yours.
3. **Iterate.** Push a change, then click **Redeploy** on the **Deployments** tab. The link stays the same, and the dashboard can filter and compare feedback by agent version.
4. **Tear down.** Click **Terminate**. A feedback stack's responses are exported first, and stay downloadable from the **Deployments** tab.

<p align="center"><img src="docs/images/deploy.jpg" alt="The MaidenRun deploy page" width="720" /></p>

## Make your agent compatible

MaidenRun runs your repo with one command, so it needs:

- **One entry point at the repo root:** a `compose.yaml` / `docker-compose.yml`, or a single `Dockerfile`.
- **The agent's UI on port 8001.** Ports 8002 and 8003 are reserved for the feedback form and dashboard.

**Check** in the app tells you what's missing. To fix a repo automatically, give [adapt.md](backend/app/templates/adapt.md) to your coding agent.

## How it works

```mermaid
flowchart LR
    You(["You"]) --> MR["MaidenRun<br/>on your machine"]
    subgraph AWS["Your AWS account"]
        Agent["Agent<br/>UI on :8001"]
        FB["Feedback stack<br/>form :8002<br/>dashboard :8003"]
    end
    MR -- deploy · redeploy --> Agent
    MR -- deploy --> FB
    Agent -- shareable link --> Testers(["Testers"])
    FB -- feedback form --> Testers

    classDef violet fill:#f1e9ff,stroke:#7c3aed,color:#3d2467
    classDef coral fill:#ffe4ec,stroke:#fb6f92,color:#3d2467
    class MR,Agent,FB violet
    class You,Testers coral
    style AWS fill:#faf9fc,stroke:#ddccfb
```

MaidenRun itself runs on your machine and uses boto3 to launch EC2 instances in your account. The agent instance clones your repo at the exact commit you checked and runs `docker compose up`.

## Cost

You pay AWS directly. A `t3.small` agent costs about **$0.026/hour** (≈ $19/month if left running), plus a little for its disk. A feedback stack costs the same. MaidenRun shows a running cost for every deployment, and tearing down stops all charges.

## Project structure

```
backend/          FastAPI control plane: validates repos, deploys to AWS, tracks cost
frontend/         React UI (the screenshots above)
feedback-stack/   The feedback form, dashboard and Postgres shipped to each feedback instance
infra/            The IAM policies for the one-time AWS setup
fixtures/         Sample repos the tests validate against
```

## Development

```bash
cd backend && uv sync --extra dev && uv run pytest              # control plane
cd feedback-stack/backend && uv sync --extra dev && uv run pytest  # needs Docker for Postgres
cd frontend && npm ci && npm run dev
uvx ruff@0.16.10 check backend feedback-stack/backend            # lint, same as CI
```

Issues and pull requests are welcome.

## License

[Apache 2.0](LICENSE)
