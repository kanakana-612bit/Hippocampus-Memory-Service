"""New contracts only. No imports, aliases, or fallback into previous APIs."""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(strict=True, min_length=1)]
Positive = Annotated[int, Field(strict=True, ge=1)]
Weight = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]
Resistance = Annotated[float, Field(strict=True, ge=-1, le=1, allow_inf_nan=False)]
AccessScope = Literal["public", "personal", "private", "restricted"]
TimeQuality = Literal["exact", "estimated_uniform", "approximate_range", "unknown"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Identity(Contract):
    principal_id: Text
    permissions: frozenset[str] = frozenset()
    audience: Literal["owner", "guest", "public"] = "owner"
    trusted_private: bool = False
    surface_id: Text = "synthetic-surface"
    destination: Text = "local"


class SourceRegister(Contract):
    id: Text
    original_name: Text
    data_class: Literal["synthetic"]
    access_scope: AccessScope = "private"
    allowed_destinations: tuple[Text, ...] = ("local",)
    timezone: str | None = None


class SourceRange(Contract):
    source_id: Text
    first: Positive
    last: Positive
    context_first: Positive | None = None
    context_last: Positive | None = None

    @model_validator(mode="after")
    def ordered(self):
        if self.last < self.first:
            raise ValueError("Inverted write range")
        if (self.context_first is None) != (self.context_last is None):
            raise ValueError("Both context range endpoints are required")
        if self.context_first is not None and not self.context_first <= self.first <= self.last <= self.context_last:
            raise ValueError("Context range must contain the write range")
        return self


class ModelConfig(Contract):
    id: Text
    provider: Literal["fixture", "openai_compatible"]
    model: Text
    destination: Text = "local"
    endpoint: str | None = None
    context_window: Annotated[int, Field(strict=True, ge=512)] = 32768
    output_reserve: Annotated[int, Field(strict=True, ge=64)] = 4096
    temperature: Annotated[float, Field(ge=0, le=2)] = 0
    revision: Text = "explicit-unreported"
    tokenizer: Literal["utf8_bytes_upper_bound"] = "utf8_bytes_upper_bound"

    @model_validator(mode="after")
    def available_capacity(self):
        if self.output_reserve >= self.context_window:
            raise ValueError("Output reserve must leave positive input capacity")
        if self.provider == "openai_compatible":
            from urllib.parse import urlparse
            parsed = urlparse(self.endpoint or "")
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("Explicit credential-free HTTP provider endpoint required")
        return self


class TimeInterpretation(Contract):
    event_time: str | None = None
    time_quality: TimeQuality
    timezone: str | None = None
    timezone_source: str | None = None
    original_datetime: str | None = None
    original_precision: str | None = None
    approximate_period: str | None = None
    relative_offset_seconds: float | None = None
    method: Literal["none", "uniform_session", "assumed_two_minutes", "explicit_review"] = "none"
    policy_version: Text = "time.v1"
    uncertainty_seconds: float | None = None
    estimation_precision: str | None = None
    range_start: str | None = None
    range_end: str | None = None
    evidence: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def quality(self):
        if (self.event_time is not None) != (self.time_quality in ("exact", "estimated_uniform")):
            raise ValueError("Time quality and point time disagree")
        if self.time_quality == "approximate_range" and not self.approximate_period:
            raise ValueError("Approximate period is required")
        if self.event_time is not None:
            from datetime import datetime
            value = datetime.fromisoformat(self.event_time)
            if value.tzinfo is None:
                raise ValueError("Point time requires an explicit offset")
        return self


class SourceSpan(Contract):
    event_id: Text
    version: Positive = 1
    source_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    start: Annotated[int, Field(strict=True, ge=0)]
    end: Positive
    quote: Text
    role: Literal["user", "assistant", "system", "developer", "tool"]

    @model_validator(mode="after")
    def length(self):
        if self.end - self.start != len(self.quote):
            raise ValueError("Span must be a half-open Unicode codepoint range")
        return self


class Prospective(Contract):
    basis: Literal["time_based", "event_based"] | None = None
    element_type: Literal["intent", "task", "plan"] | None = None
    state: Literal["planned", "in_progress", "completed", "cancelled", "suspended"] | None = None


class MemoryCandidate(Contract):
    candidate_id: Text
    kind: Literal["memory", "summary", "episode", "knowledge_candidate"]
    memory_type: Literal["episodic", "semantic", "prospective", "procedural", "embodied"] | None = None
    content: Text
    source_spans: tuple[SourceSpan, ...] = Field(min_length=1)
    retrieval_weight: Weight = .5
    generalization_weight: Weight = 0
    salience: Weight = .5
    epistemic_status: Literal["unassessed"] = "unassessed"
    prospective: Prospective | None = None
    supersedes_id: str | None = None


class ContextCandidate(Contract):
    candidate_id: Text
    name: Text
    description: Text
    source_spans: tuple[SourceSpan, ...] = Field(min_length=1)


class MembershipCandidate(Contract):
    event_id: str | None = None
    memory_candidate_id: str | None = None
    context_id: str | None = None
    context_candidate_id: str | None = None
    membership_weight: Weight
    confidence: Weight | None = None
    source_spans: tuple[SourceSpan, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def one_target(self):
        if (self.event_id is None) == (self.memory_candidate_id is None):
            raise ValueError("Exactly one membership target is required")
        if (self.context_id is None) == (self.context_candidate_id is None):
            raise ValueError("Exactly one context reference is required")
        return self


class AnalysisOutput(Contract):
    schema_version: Literal["hippocampus.analysis.v1"] = "hippocampus.analysis.v1"
    memories: tuple[MemoryCandidate, ...] = ()
    contexts: tuple[ContextCandidate, ...] = ()
    memberships: tuple[MembershipCandidate, ...] = ()


class EthicalWeights(Contract):
    privacy_resistance: Resistance | None = None
    harm_resistance: Resistance | None = None
    social_context_resistance: Resistance | None = None
    identity_sensitivity: Resistance | None = None
    external_output_resistance: Resistance | None = None


class Condition(Contract):
    """Typed AND/OR tree; no free-form scripts or eval."""
    op: Literal["all", "any", "room", "audience", "hour_range", "weekday"]
    children: tuple["Condition", ...] = ()
    value: str | None = None
    start_hour: Annotated[int, Field(strict=True, ge=0, le=23)] | None = None
    end_hour: Annotated[int, Field(strict=True, ge=0, le=23)] | None = None
    timezone: str | None = None
    weekdays: tuple[Annotated[int, Field(strict=True, ge=0, le=6)], ...] = ()

    @model_validator(mode="after")
    def shape(self):
        from zoneinfo import ZoneInfo
        if self.op in ("all", "any"):
            if not self.children or self.value is not None or self.timezone or self.weekdays or self.start_hour is not None or self.end_hour is not None:
                raise ValueError("AND/OR requires only nonempty child conditions")
        else:
            if self.children:
                raise ValueError("Leaf conditions cannot have children")
            if self.op in ("room", "audience"):
                if not self.value or self.timezone or self.weekdays or self.start_hour is not None or self.end_hour is not None:
                    raise ValueError("Room/audience requires only its value")
                if self.op == "audience" and self.value not in ("owner", "guest", "public"):
                    raise ValueError("Unknown audience")
            else:
                if not self.timezone or self.value is not None:
                    raise ValueError("Time predicates require an explicit timezone")
                ZoneInfo(self.timezone)
                if self.op == "hour_range" and (self.start_hour is None or self.end_hour is None or self.weekdays):
                    raise ValueError("Hour range requires both endpoints")
                if self.op == "weekday" and (not self.weekdays or self.start_hour is not None or self.end_hour is not None):
                    raise ValueError("Weekday requires weekdays only")
        return self
