from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

import requests
import structlog
from github import Github
from github.ContentFile import ContentFile
from github.GithubException import GithubException
from github.Repository import Repository

from app.config import Settings
from app.models import CandidateChange, DiffProposal

logger = structlog.get_logger(__name__)


@dataclass
class GithubNotifier:
    settings: Settings

    def __post_init__(self) -> None:
        self._client = Github(self.settings.github_token)
        self._repo: Repository = self._client.get_repo(self.settings.github_repo)

    def load_tool_source(self, tool_name: str, ref: str | None = None) -> tuple[str, str, str]:
        """
        Returns (file_path, source_content, blob_sha) for the target tool.
        """
        target_file = self._find_tool_file(tool_name=tool_name, ref=ref)
        file_content = self._repo.get_contents(target_file, ref=ref or self.settings.github_default_branch)
        if not isinstance(file_content, ContentFile):
            raise ValueError(f"Unable to read tool source file: {target_file}")
        source_bytes = file_content.decoded_content
        return target_file, source_bytes.decode("utf-8"), file_content.sha

    def create_candidate_change(self, tool_name: str, proposal: DiffProposal) -> CandidateChange:
        branch_name = self._create_temp_branch(tool_name=tool_name)
        target_path = self._resolve_target_path(tool_name=tool_name, proposal_path=proposal.file_path)

        target_file = self._repo.get_contents(target_path, ref=branch_name)
        if not isinstance(target_file, ContentFile):
            raise ValueError(f"Unexpected GitHub content type for {target_path}.")

        current_content = target_file.decoded_content.decode("utf-8")
        updated_content = self.apply_atomic_diff(current_content=current_content, diff=proposal.diff)

        commit_message = f"chore(eval-healer): candidate fix for {tool_name}"
        commit = self._repo.update_file(
            path=target_path,
            message=commit_message,
            content=updated_content,
            sha=target_file.sha,
            branch=branch_name,
        )
        commit_sha = commit["commit"].sha
        logger.info("candidate_branch_updated", tool_name=tool_name, branch=branch_name, path=target_path)

        return CandidateChange(
            tool_name=tool_name,
            file_path=target_path,
            branch_name=branch_name,
            commit_sha=commit_sha,
            proposal=proposal,
        )

    def create_draft_pr(
        self,
        *,
        candidate: CandidateChange,
        old_score: float,
        new_score: float,
        reasoning: str,
    ) -> str:
        improvement = new_score - old_score
        pr_title = f"[eval-healer] Improve {candidate.tool_name} score"
        pr_body = (
            "## Eval-Healer Proposal\n\n"
            f"- Tool: `{candidate.tool_name}`\n"
            f"- File: `{candidate.file_path}`\n"
            f"- Old score: `{old_score:.4f}`\n"
            f"- New score: `{new_score:.4f}`\n"
            f"- Improvement: `{improvement:.4f}`\n\n"
            "## Claude Reasoning\n\n"
            f"{reasoning}\n\n"
            "## Diff\n\n"
            f"```\n{candidate.proposal.diff}\n```\n\n"
            "_This PR is draft-only and requires human approval. It is never auto-merged._"
        )

        pull = self._repo.create_pull(
            title=pr_title,
            body=pr_body,
            head=candidate.branch_name,
            base=self.settings.github_default_branch,
            draft=True,
        )
        logger.info("draft_pr_created", tool_name=candidate.tool_name, pr_url=pull.html_url)
        return pull.html_url

    def send_slack_notification(
        self,
        *,
        tool_name: str,
        old_score: float,
        new_score: float,
        pr_url: str,
        run_id: str,
    ) -> None:
        payload: dict[str, Any] = {
            "text": (
                f"eval-healer improved `{tool_name}` from {old_score:.3f} to {new_score:.3f}. "
                f"Draft PR: {pr_url} (run: {run_id})"
            )
        }
        try:
            response = requests.post(
                self.settings.slack_webhook_url,
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
        except requests.RequestException as exc:  # pragma: no cover - network
            logger.exception("slack_notification_failed", tool_name=tool_name, error=str(exc))

    def _create_temp_branch(self, tool_name: str) -> str:
        base = self._repo.get_branch(self.settings.github_default_branch)
        safe_tool = tool_name.replace("/", "-").replace("_", "-")
        suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        branch_name = f"eval-healer/{safe_tool}/{suffix}"
        ref = f"refs/heads/{branch_name}"
        try:
            self._repo.create_git_ref(ref=ref, sha=base.commit.sha)
        except GithubException as exc:
            raise RuntimeError(f"Failed to create branch {branch_name}: {exc.data}") from exc
        return branch_name

    def _resolve_target_path(self, tool_name: str, proposal_path: str) -> str:
        normalized = proposal_path.strip()
        if normalized and normalized != ".":
            if normalized.startswith(self.settings.github_tools_path):
                return normalized
            joined = str(PurePosixPath(self.settings.github_tools_path) / normalized)
            return joined
        return self._find_tool_file(tool_name=tool_name, ref=self.settings.github_default_branch)

    def _find_tool_file(self, tool_name: str, ref: str | None) -> str:
        base_path = self.settings.github_tools_path.strip("/")
        target_filename = f"{tool_name}.py"
        queue: list[str] = [base_path]

        while queue:
            path = queue.pop(0)
            contents = self._repo.get_contents(path, ref=ref or self.settings.github_default_branch)
            if isinstance(contents, ContentFile):
                if contents.path.endswith(target_filename):
                    return contents.path
                continue

            for item in contents:
                if item.type == "dir":
                    queue.append(item.path)
                elif item.type == "file" and item.name == target_filename:
                    return item.path

        raise FileNotFoundError(
            f"Could not locate file for tool '{tool_name}' under GITHUB_TOOLS_PATH={self.settings.github_tools_path}"
        )

    @staticmethod
    def apply_atomic_diff(*, current_content: str, diff: str) -> str:
        """
        Applies one atomic search/replace diff.
        Format:
        <<<SEARCH
        old text
        ===
        new text
        >>>REPLACE
        """
        start_token = "<<<SEARCH\n"
        middle_token = "\n===\n"
        end_token = "\n>>>REPLACE"
        if start_token not in diff or middle_token not in diff or end_token not in diff:
            raise ValueError("Diff format invalid; expected SEARCH/REPLACE block.")

        body = diff.split(start_token, 1)[1]
        old_and_new = body.split(end_token, 1)[0]
        old_text, new_text = old_and_new.split(middle_token, 1)
        if old_text not in current_content:
            raise ValueError("SEARCH block not found in source content.")
        return current_content.replace(old_text, new_text, 1)

