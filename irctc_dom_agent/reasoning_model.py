from __future__ import annotations

import os
from pathlib import Path

from .action_space import ActionSpace
from .state import Decision, EscalationContext, State


class ReasoningUnavailable(RuntimeError):
    """Raised when the Jev-backed reasoning layer can't be reached: missing
    API key, missing SDK, or an API-level failure. Callers must treat this as
    a pause trigger, not silently fall back to guessing.
    """


_dotenv_loaded = False


def _load_dotenv_once() -> None:
    """Minimal .env loader (no new dependency): reads irctc-dom-agent/.env if
    present. Also accepts JEV_API_KEY as an alias for TYPESAFE_API_KEY, since
    that's what naturally gets written when the model is called "Jev" - the
    SDK itself only recognizes TYPESAFE_API_KEY.
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True

    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)

    if "TYPESAFE_API_KEY" not in os.environ and "JEV_API_KEY" in os.environ:
        os.environ["TYPESAFE_API_KEY"] = os.environ["JEV_API_KEY"]


def _client():
    _load_dotenv_once()
    if not os.environ.get("TYPESAFE_API_KEY"):
        raise ReasoningUnavailable("TYPESAFE_API_KEY is not set; cannot call Jev for reasoning.")
    try:
        from typesafe_sdk import TypeSafeClient, TypeSafeError
    except ImportError as exc:
        raise ReasoningUnavailable("typesafe-sdk is not installed (`pip install typesafe-sdk`).") from exc
    try:
        return TypeSafeClient()
    except TypeSafeError as exc:
        raise ReasoningUnavailable(f"Could not create TypeSafe client: {exc}") from exc


def resolve_ambiguous_element(state: State, action_space: ActionSpace, context: EscalationContext) -> Decision:
    """System-2 use (b): decision_model.py returned no confident match this
    tick. Ask Jev's Choice primitive to pick the element that best advances
    `context.intent`, over context.candidate_ids only - decision_model.py
    (via escalation_context) is responsible for scoping those candidates to
    what's actually relevant, and for saying what to *do* once one is picked
    (context.action / context.text), since Jev only judges which element,
    not what action the current step calls for.
    """
    from typesafe_sdk import Choice, TypeSafeError

    elements_by_id = {e.id: e for e in action_space.elements}
    criteria = {
        str(cid): (elements_by_id[cid].name or f"unlabeled {elements_by_id[cid].role}")
        for cid in context.candidate_ids
        if cid in elements_by_id
    }
    if not criteria:
        raise ReasoningUnavailable(f"No candidate elements available for: {context.intent}")

    try:
        with _client() as client:
            response = client.system_one(
                state={
                    "user_goal": state.user_goal,
                    "current_workflow_step": state.workflow_step.name,
                    "intent": context.intent,
                },
                questions={
                    "pick": Choice(
                        instructions=f"Pick the element that best accomplishes: {context.intent}",
                        criteria=criteria,
                    ),
                },
            )
    except TypeSafeError as exc:
        raise ReasoningUnavailable(f"Jev request failed: {exc}") from exc

    answer = response.answers["pick"]
    picked_id = int(answer.choice)
    picked_el = elements_by_id[picked_id]
    return Decision(
        action=context.action,
        target=picked_id,
        text=context.text,
        snapshot_id=action_space.snapshot_id,
        confidence=answer.confidence,
        source="system2",
        rationale=f"Jev selected {picked_el.name!r} for: {context.intent} (confidence {answer.confidence:.2f})",
    )
