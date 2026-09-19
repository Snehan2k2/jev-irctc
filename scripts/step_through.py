"""Phase B demo: heuristic decision_model + executor + safety, one workflow
step at a time, printing each Decision before it executes. No reasoning
layer wired in here - an ambiguous match just gets logged as "would
escalate to Jev" so this script can run without a TYPESAFE_API_KEY.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from irctc_dom_agent import decision_model, executor, safety
from irctc_dom_agent.browser import launch_page
from irctc_dom_agent.dom_indexer import DomIndexer
from irctc_dom_agent.state import State, WorkflowStep, parse_goal

START_URL = "https://www.irctc.co.in/nget/train-search"
GOAL = "Find a 3A train from Chennai to Bangalore on 15/10/2026"

DEMO_STEPS = [
    WorkflowStep.FILL_FROM,
    WorkflowStep.CONFIRM_FROM_SUGGESTION,
    WorkflowStep.FILL_TO,
    WorkflowStep.CONFIRM_TO_SUGGESTION,
    WorkflowStep.FILL_DATE,
    WorkflowStep.SEARCH,
]


def main() -> None:
    state = State(user_goal=GOAL, slots=parse_goal(GOAL))
    indexer = DomIndexer()
    stale_seen = False
    step_index = 0

    with launch_page() as page:
        page.goto(START_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        for _ in range(40):
            space = indexer.build(page)

            trigger = safety.scan(page, space)
            if trigger:
                safety.explain_and_pause(trigger, state)
                break

            if state.workflow_step == WorkflowStep.OPEN and not space.blocking_dialog:
                state.workflow_step = DEMO_STEPS[step_index]

            decision = decision_model.decide(state, space)
            if decision is None:
                if space.blocking_dialog:
                    print("[OPEN] no confident dialog-dismiss match; would escalate to Jev here.")
                else:
                    print(f"[{state.workflow_step.name}] ambiguous/no confident match; would escalate to Jev here.")
                break

            print(f"[{state.workflow_step.name}] {decision.action} -> target={decision.target} ({decision.rationale})")

            try:
                executor.perform(page, space, decision)
            except executor.StaleSnapshotError as exc:
                stale_seen = True
                print(f"  StaleSnapshotError (expected at least once, e.g. after the autocomplete re-renders): {exc}")
                continue
            except executor.ActionFailed as exc:
                print(f"  ActionFailed (e.g. an overlapping element blocked the click): {exc}")
                continue

            if space.blocking_dialog or decision.action == "WAIT":
                page.wait_for_timeout(400)
                continue

            step_index += 1
            if step_index >= len(DEMO_STEPS):
                break
            state.workflow_step = DEMO_STEPS[step_index]
            page.wait_for_timeout(500)

        print(f"\nStaleSnapshotError observed during this run: {stale_seen}")
        page.wait_for_timeout(3000)


if __name__ == "__main__":
    main()
