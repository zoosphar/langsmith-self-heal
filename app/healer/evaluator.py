from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

import structlog
from github import Github
from github.Repository import Repository
from github.Workflow import Workflow

from app.config import Settings
from app.healer.fetcher import LangSmithFetcher
from app.models import EvalMode, EvalRunResult, ToolScore

logger = structlog.get_logger(__name__)


@dataclass
class Evaluator:
    settings: Settings
    fetcher: LangSmithFetcher

    def __post_init__(self) -> None:
        self._github = Github(self.settings.github_token)
        self._repo: Repository = self._github.get_repo(self.settings.github_repo)

    async def run_and_fetch_scores(
        self,
        *,
        tool_name: str,
        branch_ref: str | None = None,
    ) -> tuple[EvalRunResult, dict[str, ToolScore]]:
        if self.settings.eval_mode == EvalMode.LOCAL:
            result = await self._run_local_mode(tool_name=tool_name)
        else:
            result = await self._run_actions_mode(tool_name=tool_name, branch_ref=branch_ref)

        await asyncio.sleep(self.settings.post_eval_langsmith_wait_seconds)
        if self.settings.eval_mode == EvalMode.GITHUB_ACTIONS:
            await asyncio.sleep(self.settings.github_actions_extra_langsmith_wait_seconds)

        scores = await asyncio.to_thread(self.fetcher.fetch_scores)
        return result, scores

    async def _run_local_mode(self, *, tool_name: str) -> EvalRunResult:
        completed = await asyncio.to_thread(
            subprocess.run,
            [self.settings.eval_script_path, "--tool", tool_name],
            capture_output=True,
            text=True,
            check=False,
        )
        success = completed.returncode == 0
        details = (completed.stdout or "").strip()
        if not success:
            details = f"{details}\n{(completed.stderr or '').strip()}".strip()
            logger.error("local_mode_failed", tool_name=tool_name, returncode=completed.returncode, details=details)

        return EvalRunResult(
            mode=EvalMode.LOCAL,
            succeeded=success,
            run_identifier=f"local:{tool_name}:{int(datetime.now(tz=timezone.utc).timestamp())}",
            details=details or None,
        )

    async def _run_actions_mode(self, *, tool_name: str, branch_ref: str | None) -> EvalRunResult:
        workflow = await asyncio.to_thread(self._get_workflow)
        dispatch_ref = branch_ref or self.settings.github_eval_workflow_ref
        dispatched = await asyncio.to_thread(
            workflow.create_dispatch,
            dispatch_ref,
            {"tool_name": tool_name},
        )
        if not dispatched:
            return EvalRunResult(
                mode=EvalMode.GITHUB_ACTIONS,
                succeeded=False,
                run_identifier=None,
                details="workflow_dispatch returned False",
            )

        start_time = datetime.now(timezone.utc)
        run_id, conclusion = await self._poll_workflow_run(
            workflow=workflow,
            dispatch_ref=dispatch_ref,
            started_after=start_time,
        )
        success = conclusion == "success"
        details = f"Workflow run {run_id} concluded with status={conclusion}"

        return EvalRunResult(
            mode=EvalMode.GITHUB_ACTIONS,
            succeeded=success,
            run_identifier=str(run_id),
            details=details,
        )

    def _get_workflow(self) -> Workflow:
        return self._repo.get_workflow(self.settings.github_actions_workflow)

    async def _poll_workflow_run(
        self,
        *,
        workflow: Workflow,
        dispatch_ref: str,
        started_after: datetime,
    ) -> tuple[int, str]:
        timeout_seconds = self.settings.github_eval_timeout_seconds
        interval = self.settings.github_eval_poll_interval_seconds
        elapsed = 0

        while elapsed <= timeout_seconds:
            runs = await asyncio.to_thread(workflow.get_runs, branch=dispatch_ref, event="workflow_dispatch")
            run = next(
                (
                    item
                    for item in runs
                    if item.created_at and self._normalize_ts(item.created_at) >= self._normalize_ts(started_after)
                ),
                None,
            )
            if run and run.status == "completed":
                return int(run.id), str(run.conclusion)
            await asyncio.sleep(interval)
            elapsed += interval

        raise TimeoutError(f"Timed out waiting for workflow {self.settings.github_actions_workflow} completion.")

    @staticmethod
    def _normalize_ts(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

