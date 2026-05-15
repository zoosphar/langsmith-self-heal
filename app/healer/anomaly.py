from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AnomalyDetector:
    """
    Flags tools that remain at 0% across repeated attempts while peers improve.
    """

    _zero_score_attempts: dict[str, int] = field(default_factory=dict)
    _flagged_tools: set[str] = field(default_factory=set)

    def should_skip(self, tool_name: str) -> bool:
        return tool_name in self._flagged_tools

    def observe_attempt(self, *, tool_name: str, score: float, peers_improved: bool) -> bool:
        if score <= 0.0:
            self._zero_score_attempts[tool_name] = self._zero_score_attempts.get(tool_name, 0) + 1
        else:
            self._zero_score_attempts[tool_name] = 0

        if self._zero_score_attempts.get(tool_name, 0) >= 2 and peers_improved:
            self._flagged_tools.add(tool_name)
            return True
        return False

    def get_flagged_tools(self) -> list[str]:
        return sorted(self._flagged_tools)

