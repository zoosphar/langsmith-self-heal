from __future__ import annotations

from app.models import BucketType, FailingExample


class FailureBucketer:
    """Heuristic classifier that maps failures into repair buckets."""

    def classify(self, failure: FailingExample) -> BucketType:
        text = " ".join(
            value.lower()
            for value in (
                failure.error_message,
                failure.actual_output,
                failure.expected_output,
            )
            if value
        )

        if any(token in text for token in ("jsonschema", "schema", "validationerror", "invalid type")):
            return BucketType.TOOL_SCHEMA

        if any(token in text for token in ("prompt", "instruction", "formatting", "hallucinat")):
            return BucketType.PROMPT

        if any(token in text for token in ("description", "docstring", "tool doc", "capability")):
            return BucketType.TOOL_DESCRIPTION

        return BucketType.WIRING_CODE

