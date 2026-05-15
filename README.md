# langsmith-eval-healer

![Works with any LangSmith project](https://img.shields.io/badge/Works_with-any_LangSmith_project-4c1?style=for-the-badge)

Self-healing LangSmith eval pipeline service. It runs on FastAPI + APScheduler inside one Docker container, proposes atomic fixes with Claude Sonnet, validates score improvements, opens draft PRs, and posts Slack notifications. Human review is always required.

# Why I Built This

At AdQuick, I was maintaining an AI Copilot with 53 tool calls.

When evals started failing, debugging why an LLM picks the wrong 
tool was a manual nightmare — reading traces, tweaking prompts, 
updating tool descriptions, rerunning evals. Each tool took 2-3 
days to fix.

We built a golden dataset for each tool using LLM-assisted examples. 
Fast to build — but LLMs generate subtly wrong examples too. Our 
LLM-as-judge score came back at 63%. No systematic way to find what 
was broken.

So I built this. A nightly loop that diagnoses failures, asks Claude 
Sonnet for one precise atomic fix, reruns evals, and drafts a PR if 
the score improves. Humans always approve.

After the first run, score jumped to 80%+.

The unexpected insight: 3 tools stayed at exactly 0% while everything 
else improved. That shouldn't happen. Zero movement meant the golden 
dataset examples themselves were wrong — not the code. The self-healer 
exposed bad training data by elimination.

## One-click Deploy

```bash
git clone https://github.com/zoosphar/langsmith-self-heal
cp healer/.env.example healer/.env
nano healer/.env
docker-compose -f healer/docker-compose.yml up -d
```

## How It Works

1. Nightly cron in-container triggers `/heal` logic.
2. Service fetches current LangSmith scores and failing traces/examples.
3. Failures are bucketed into `prompt`, `tool_schema`, `tool_description`, `wiring_code`.
4. Claude Sonnet returns one atomic diff proposal.
5. Service reads tool source directly via GitHub API (`GITHUB_TOOLS_PATH`), creates candidate branch commit(s), and runs eval rerun mode:
   - `EVAL_MODE=local`: runs `EVAL_SCRIPT_PATH --tool <tool_name>`.
   - `EVAL_MODE=github_actions`: dispatches `GITHUB_ACTIONS_WORKFLOW` with `tool_name` input and polls completion every 30s.
6. Service waits before score read (`POST_EVAL_LANGSMITH_WAIT_SECONDS`, plus GitHub Actions extra wait) to avoid stale LangSmith scores.
7. If score improvement is `>= HEAL_THRESHOLD`, it drafts a PR and sends Slack notification.
8. Nothing auto-merges.

## Endpoint Reference

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/heal` | Trigger full heal run manually. |
| POST | `/heal/{tool_name}` | Trigger heal run for one tool. |
| GET | `/status` | Return last run summary and per-tool results. |
| GET | `/scores` | Return latest observed eval scores per tool. |
| GET | `/anomalies` | Return tools flagged as bad examples (stuck at 0%). |

## Cron Behavior (No External Infra)

- APScheduler runs inside the same FastAPI container.
- Cron expression is configured via `CRON_SCHEDULE`.
- On startup, the app logs the next scheduled run time.
- No Redis, Celery, Kubernetes CronJob, or external scheduler is required.

## Architecture (ASCII)

```text
                  +-------------------------+
                  |  FastAPI + APScheduler  |
                  |  /heal + nightly cron   |
                  +-----------+-------------+
                              |
                              v
                    +-------------------+
                    | LangSmith Fetcher |
                    | scores + failures |
                    +---------+---------+
                              |
                              v
                    +-------------------+
                    | Failure Bucketer  |
                    +---------+---------+
                              |
                              v
                    +-------------------+
                    | Claude Suggester  |
                    | atomic diff JSON  |
                    +---------+---------+
                              |
                              v
                    +-------------------+
                    | GitHub Notifier   |
                    | read source +     |
                    | candidate branch  |
                    +---------+---------+
                              |
                              v
                    +-------------------+
                    | Evaluator         |
                    | local or actions  |
                    +---------+---------+
                              |
                              v
                    +-------------------+
                    | Compare Scores    |
                    | threshold check   |
                    +---------+---------+
                              |
                 +------------+-------------+
                 |                          |
                 v                          v
      +----------------------+   +----------------------+
      | Draft GitHub PR      |   | Skip / Continue Tool |
      | + Slack Notification |   | (graceful failure)   |
      +----------------------+   +----------------------+
```

## Environment Variables

Copy `.env.example` to `.env` and set:

- `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT_NAME`, `LANGSMITH_DATASET_NAME`
- `ANTHROPIC_API_KEY`
- `GITHUB_TOKEN`, `GITHUB_REPO`, `GITHUB_TOOLS_PATH`, `GITHUB_DEFAULT_BRANCH`
- `EVAL_MODE` (`local` or `github_actions`)
- `EVAL_SCRIPT_PATH` (used in local mode)
- `GITHUB_ACTIONS_WORKFLOW`, `GITHUB_EVAL_WORKFLOW_REF`, `GITHUB_EVAL_POLL_INTERVAL_SECONDS`, `GITHUB_EVAL_TIMEOUT_SECONDS`
- `POST_EVAL_LANGSMITH_WAIT_SECONDS`, `GITHUB_ACTIONS_EXTRA_LANGSMITH_WAIT_SECONDS`
- `SLACK_WEBHOOK_URL`
- `HEAL_THRESHOLD`, `CRON_SCHEDULE`

## Local Development

```bash
cd healer
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## Let's Talk Evals

Building agentic AI workflows and struggling with eval coverage?

I help teams design and implement eval pipelines for production 
agentic systems with 10+ tools.

reach out: avrlsngh@gmail.com
