"""One-off recorder for the README demo GIF. Not part of the agent itself -
records a real run (through the Search click) to a .webm video via
Playwright's built-in video capture, for later conversion to a GIF.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

from irctc_dom_agent import agent_loop
from irctc_dom_agent.dom_indexer import DomIndexer
from irctc_dom_agent.state import State, WorkflowStep, parse_goal

GOAL = "Find a 3A train from Chennai to Bangalore on 15/10/2026, preferably departing in the evening"
OUT_DIR = Path(__file__).resolve().parent.parent / "recordings"
STOP_AFTER = WorkflowStep.SEARCH  # stop once this step's action has executed


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    state = State(user_goal=GOAL, slots=parse_goal(GOAL))
    indexer = DomIndexer()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1000, "height": 650},
            record_video_dir=str(OUT_DIR),
            record_video_size={"width": 1000, "height": 650},
        )
        page = context.new_page()
        page.goto(agent_loop.DEFAULT_START_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        for _ in range(40):
            if not agent_loop._run_iteration(page, indexer, state):
                break
            if state.workflow_step in (STOP_AFTER, WorkflowStep.IDENTIFY_TRAIN):
                page.wait_for_timeout(1500)
                break

        context.close()
        browser.close()

    video_files = sorted(OUT_DIR.glob("*.webm"), key=lambda p: p.stat().st_mtime)
    print(f"Recorded: {video_files[-1] if video_files else 'no video found'}")


if __name__ == "__main__":
    main()
