"""Phase C: full agent loop, including the Jev-backed reasoning layer.
Requires TYPESAFE_API_KEY in the environment. Runs through step 9
(identify a matching train/class) and stops - never clicks toward booking.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from irctc_dom_agent.agent_loop import run

DEFAULT_GOAL = "Find a 3A train from Chennai to Bangalore on 15/10/2026, preferably departing in the evening"


def main() -> None:
    goal = " ".join(sys.argv[1:]) or DEFAULT_GOAL
    print(f"Goal: {goal}\n")
    state = run(goal)
    print("\n--- final state ---")
    print(f"workflow_step = {state.workflow_step.name}")
    print(f"pause_reason  = {state.pause_reason}")

    result = state.result or {}
    preferred = result.get("preferred_departure_minutes")
    top = result.get("top_available")
    if top:
        priority = "closest departure time, then duration" if preferred is not None else "duration only"
        print(
            f"\n{len(top)} confirmed-available train(s) for {result.get('requested_class')} "
            f"(checked {result.get('checked_count')} trains, priority: {priority}):"
        )
        for i, r in enumerate(top, start=1):
            print(
                f"  {i}. {r['name']} ({r['number']}) - {r['departure']} -> {r['arrival']}, "
                f"duration {r['duration']}, {r['availability']}"
            )
    elif result:
        print(f"\nresult = {result}")


if __name__ == "__main__":
    main()
