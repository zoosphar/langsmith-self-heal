from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

import structlog

from app.config import Settings
from app.healer.anomaly import AnomalyDetector
from app.healer.bucketer import FailureBucketer
from app.healer.evaluator import Evaluator
from app.healer.fetcher import LangSmithFetcher
from app.healer.notifier import GithubNotifier
from app.healer.suggester import ClaudeSuggester
from app.models import EvalMode, HealRunState, ToolHealResult, ToolScore

logger = structlog.get_logger(__name__)


@dataclass
class HealerOrchestrator:
    settings: Settings
    fetcher: LangSmithFetcher
    bucketer: FailureBucketer
    suggester: ClaudeSuggester
    evaluator: Evaluator
    notifier: GithubNotifier
    anomaly_detector: AnomalyDetector
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _last_run: HealRunState | None = None
    _latest_scores: dict[str, ToolScore] = field(default_factory=dict)

    async def heal(self, tool_name: str | None = None) -> HealRunState:
        if self._lock.locked():
            raise RuntimeError("A healing run is already in progress.")

        async with self._lock:
            run_id = str(uuid4())
            run_state = HealRunState(run_id=run_id, mode=self.settings.eval_mode)
            self._last_run = run_state
            logger.info("healing_run_started", run_id=run_id, tool_name=tool_name)

            try:
                baseline_scores = await asyncio.to_thread(self.fetcher.fetch_scores)
                self._latest_scores = baseline_scores
                target_tools = self._resolve_target_tools(tool_name=tool_name, scores=baseline_scores)
                peer_improved = False

                for current_tool in target_tools:
                    result = await self._heal_single_tool(
                        run_id=run_id,
                        tool_name=current_tool,
                        baseline_scores=baseline_scores,
                        peers_improved=peer_improved,
                    )
                    run_state.tool_results.append(result)
                    if (result.improvement or 0.0) > 0:
                        peer_improved = True
            except Exception as exc:
                logger.exception("healing_run_failed", run_id=run_id, error=str(exc))
                run_state.error = str(exc)
            finally:
                run_state.finished_at = datetime.utcnow()

            self._last_run = run_state
            logger.info("healing_run_finished", run_id=run_id, results=len(run_state.tool_results))
            return run_state

    async def _heal_single_tool(
        self,
        *,
        run_id: str,
        tool_name: str,
        baseline_scores: dict[str, ToolScore],
        peers_improved: bool,
    ) -> ToolHealResult:
        old_score = baseline_scores.get(tool_name, ToolScore(tool_name=tool_name, score=0.0)).score
        if self.anomaly_detector.should_skip(tool_name):
            return ToolHealResult(
                tool_name=tool_name,
                status="skipped_anomaly",
                old_score=old_score,
                anomaly_flagged=True,
            )

        try:
            failures = await asyncio.to_thread(self.fetcher.fetch_failing_examples, tool_name)
            if not failures:
                return ToolHealResult(tool_name=tool_name, status="no_failures", old_score=old_score)

            bucket = self.bucketer.classify(failures[0])
            source_file, source_code, _ = await asyncio.to_thread(self.notifier.load_tool_source, tool_name, None)

            proposal = await self.suggester.suggest_atomic_diff(
                tool_name=tool_name,
                bucket=bucket,
                file_path=source_file,
                source_code=source_code,
                failures=failures,
                current_score=old_score,
            )

            candidate = await asyncio.to_thread(self.notifier.create_candidate_change, tool_name, proposal)
            eval_ref = candidate.branch_name if self.settings.eval_mode == EvalMode.GITHUB_ACTIONS else None
            _, new_scores = await self.evaluator.run_and_fetch_scores(tool_name=tool_name, branch_ref=eval_ref)
            self._latest_scores = new_scores

            new_score = new_scores.get(tool_name, ToolScore(tool_name=tool_name, score=old_score)).score
            improvement = new_score - old_score
            flagged = self.anomaly_detector.observe_attempt(
                tool_name=tool_name,
                score=new_score,
                peers_improved=peers_improved,
            )
            if flagged:
                return ToolHealResult(
                    tool_name=tool_name,
                    status="anomaly_flagged",
                    bucket=bucket,
                    old_score=old_score,
                    new_score=new_score,
                    improvement=improvement,
                    proposal_reasoning=proposal.reasoning,
                    anomaly_flagged=True,
                )

            if improvement >= self.settings.heal_threshold:
                pr_url = await asyncio.to_thread(
                    self.notifier.create_draft_pr,
                    candidate=candidate,
                    old_score=old_score,
                    new_score=new_score,
                    reasoning=proposal.reasoning,
                )
                await asyncio.to_thread(
                    self.notifier.send_slack_notification,
                    tool_name=tool_name,
                    old_score=old_score,
                    new_score=new_score,
                    pr_url=pr_url,
                    run_id=run_id,
                )
                return ToolHealResult(
                    tool_name=tool_name,
                    status="pr_created",
                    bucket=bucket,
                    old_score=old_score,
                    new_score=new_score,
                    improvement=improvement,
                    proposal_reasoning=proposal.reasoning,
                    pr_url=pr_url,
                )

            return ToolHealResult(
                tool_name=tool_name,
                status="no_improvement",
                bucket=bucket,
                old_score=old_score,
                new_score=new_score,
                improvement=improvement,
                proposal_reasoning=proposal.reasoning,
            )
        except Exception as exc:
            logger.exception("tool_heal_failed", run_id=run_id, tool_name=tool_name, error=str(exc))
            return ToolHealResult(
                tool_name=tool_name,
                status="failed",
                old_score=old_score,
                error=str(exc),
            )

    @staticmethod
    def _resolve_target_tools(tool_name: str | None, scores: dict[str, ToolScore]) -> list[str]:
        if tool_name:
            return [tool_name]
        return sorted([name for name, score in scores.items() if score.score < 1.0])

    def get_last_run(self) -> HealRunState | None:
        return self._last_run

    def get_latest_scores(self) -> list[ToolScore]:
        return sorted(self._latest_scores.values(), key=lambda item: item.tool_name)

    def get_anomalies(self) -> list[str]:
        return self.anomaly_detector.get_flagged_tools()

    async def refresh_scores(self) -> list[ToolScore]:
        self._latest_scores = await asyncio.to_thread(self.fetcher.fetch_scores)
        return self.get_latest_scores()

