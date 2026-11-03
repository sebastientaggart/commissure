"""The outcome vocabulary: what a stage says about each subject it touched.

    valid        a payload, no reason
    warning      a payload, a reason code, and at least one diagnostic
    quarantined  no payload, a reason code
    skipped      no payload, a reason code; an explicit status, never a fabricated row

The envelope enforces these shapes, so a stage cannot return a payload with a
quarantine or a warning without saying why.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

OUTCOMES = ("valid", "warning", "quarantined", "skipped")
Outcome = Literal["valid", "warning", "quarantined", "skipped"]


class Diagnostic(BaseModel):
    """One machine-readable observation about a subject."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1)
    message: str = ""
    field: str | None = None  # dotted path into the payload, for field-level problems
    detail: dict[str, Any] = Field(default_factory=dict)


class RecordEnvelope[T](BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    outcome: Outcome
    payload: T | None = None
    reason_code: str | None = Field(default=None, min_length=1)
    diagnostics: list[Diagnostic] = Field(default_factory=list)

    @model_validator(mode="after")
    def _shape(self) -> RecordEnvelope[T]:
        has_payload = self.payload is not None
        if self.outcome in ("valid", "warning") and not has_payload:
            raise ValueError(f"a {self.outcome} envelope needs a payload")
        if self.outcome in ("quarantined", "skipped") and has_payload:
            raise ValueError(f"a {self.outcome} envelope carries no payload")
        if (self.outcome == "valid") != (self.reason_code is None):
            raise ValueError("valid has no reason code; every other outcome has one")
        if self.outcome == "warning" and not self.diagnostics:
            raise ValueError("a warning needs at least one diagnostic")
        return self

    @classmethod
    def valid(cls, payload: T) -> RecordEnvelope[T]:
        return cls(outcome="valid", payload=payload)

    @classmethod
    def warning(cls, payload: T, reason: str, *diagnostics: Diagnostic) -> RecordEnvelope[T]:
        return cls(
            outcome="warning",
            payload=payload,
            reason_code=reason,
            diagnostics=list(diagnostics) or [Diagnostic(code=reason)],
        )

    @classmethod
    def quarantined(cls, reason: str, *diagnostics: Diagnostic) -> RecordEnvelope[T]:
        return cls(outcome="quarantined", reason_code=reason, diagnostics=list(diagnostics))

    @classmethod
    def skipped(cls, reason: str, *diagnostics: Diagnostic) -> RecordEnvelope[T]:
        return cls(outcome="skipped", reason_code=reason, diagnostics=list(diagnostics))
