"""Stable policy/inspection hook contract.

These stages are the stable inspection points for SunaQ data-flow boundaries.
RC2 adds a built-in class-aware egress policy before the optional site-specific
evaluator. Research queries can therefore be guarded while model-processing and
embedding payloads remain non-destructive by default.

An optional process-local evaluator can still delegate to malware, URL-policy,
ICAP/YARA/DLP or site-specific adapters without changing calling data paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from rag.egress_policy import EMBEDDING, MODEL_PROCESSING, RESEARCH_QUERY, evaluate_egress


ALLOW = "ALLOW"
BLOCK = "BLOCK"
QUARANTINE = "QUARANTINE"
MODIFY = "MODIFY"

OUTBOUND_QUERY = "outbound_query"
PRE_FETCH = "pre_fetch"
POST_FETCH = "post_fetch"
PRE_PERSIST = "pre_persist"
PRE_MODEL_EGRESS = "pre_model_egress"

HOOK_STAGES = frozenset(
    {
        OUTBOUND_QUERY,
        PRE_FETCH,
        POST_FETCH,
        PRE_PERSIST,
        PRE_MODEL_EGRESS,
    }
)
HOOK_ACTIONS = frozenset({ALLOW, BLOCK, QUARANTINE, MODIFY})


@dataclass(frozen=True)
class PolicyHookRequest:
    stage: str
    content: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyHookDecision:
    action: str = ALLOW
    content: Any = None
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


class PolicyHookRejected(RuntimeError):
    """A configured policy hook blocked or quarantined an operation."""

    def __init__(self, stage: str, action: str, reason: str = ""):
        self.stage = stage
        self.action = action
        self.reason = reason
        detail = f"policy hook {stage} returned {action}"
        if reason:
            detail += f": {reason}"
        super().__init__(detail)


PolicyEvaluator = Callable[[PolicyHookRequest], PolicyHookDecision]
_evaluator: PolicyEvaluator | None = None


def install_policy_evaluator(evaluator: PolicyEvaluator | None) -> None:
    """Install the process-local evaluator.

    Startup/configuration code may install an adapter later. Passing None restores
    the shipped ALLOW-only behaviour.
    """

    global _evaluator
    _evaluator = evaluator


def reset_policy_evaluator() -> None:
    install_policy_evaluator(None)


def evaluate_policy_hook(
    stage: str,
    *,
    content: Any = None,
    metadata: Mapping[str, Any] | None = None,
) -> PolicyHookDecision:
    """Evaluate one hook stage.

    The built-in egress policy runs first for outbound stages. Exceptions raised
    by an optional configured evaluator are not swallowed: a required inspection
    service can therefore fail closed simply by failing the operation.
    """

    stage = str(stage or "").strip()
    if stage not in HOOK_STAGES:
        raise ValueError(f"unknown policy hook stage: {stage!r}")

    metadata_dict = dict(metadata or {})
    egress_class = str(metadata_dict.get("egress_class") or "").strip().casefold()
    if not egress_class:
        if stage == OUTBOUND_QUERY:
            egress_class = RESEARCH_QUERY
        elif stage == PRE_MODEL_EGRESS:
            kind = str(metadata_dict.get("kind") or "").strip().casefold()
            if kind == "embedding":
                egress_class = EMBEDDING
            elif kind == "delegated_research":
                egress_class = RESEARCH_QUERY
            else:
                egress_class = MODEL_PROCESSING

    builtin = None
    effective_content = content
    if egress_class:
        builtin = evaluate_egress(
            content,
            egress_class=egress_class,
            metadata=metadata_dict,
        )
        if builtin.action == BLOCK:
            return PolicyHookDecision(
                action=BLOCK,
                content=content,
                reason=builtin.reason,
                metadata={"egress_class": egress_class, "mode": builtin.mode},
            )
        effective_content = builtin.content

    request = PolicyHookRequest(
        stage=stage,
        content=effective_content,
        metadata=metadata_dict,
    )
    evaluator = _evaluator
    if evaluator is None:
        if builtin is not None and builtin.action == MODIFY:
            return PolicyHookDecision(
                action=MODIFY,
                content=builtin.content,
                metadata={
                    "egress_class": egress_class,
                    "mode": builtin.mode,
                    "redactions": builtin.redactions,
                },
            )
        return PolicyHookDecision(action=ALLOW, content=effective_content)

    decision = evaluator(request)
    if not isinstance(decision, PolicyHookDecision):
        raise TypeError("policy evaluator must return PolicyHookDecision")

    action = str(decision.action or "").strip().upper()
    if action not in HOOK_ACTIONS:
        raise ValueError(f"unknown policy hook action: {decision.action!r}")

    if action == ALLOW and builtin is not None and builtin.action == MODIFY:
        action = MODIFY
        decision_content = builtin.content
    else:
        decision_content = decision.content
    return PolicyHookDecision(
        action=action,
        content=decision_content,
        reason=str(decision.reason or ""),
        metadata=dict(decision.metadata or {}),
    )


def apply_policy_hook(
    stage: str,
    *,
    content: Any = None,
    metadata: Mapping[str, Any] | None = None,
) -> Any:
    """Apply one hook and return the permitted/transformed content."""

    decision = evaluate_policy_hook(stage, content=content, metadata=metadata)
    if decision.action in {BLOCK, QUARANTINE}:
        raise PolicyHookRejected(stage, decision.action, decision.reason)
    if decision.action == MODIFY:
        return decision.content
    return content
