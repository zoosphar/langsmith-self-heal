# eval-healer

![Works with any LangSmith project](https://img.shields.io/badge/Works_with-any_LangSmith_project-4c1?style=for-the-badge)

Self-healing LangSmith eval pipeline service. It runs on FastAPI + APScheduler inside one Docker container, proposes atomic fixes with Claude Sonnet, validates score improvements, opens draft PRs, and posts Slack notifications. Human review is always required.

## One-click Deploy

```bash
git clone <your-repo-url>
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
