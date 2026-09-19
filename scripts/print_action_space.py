"""Phase A demo: DOM indexer + action space only, no decision-making.

Opens the real IRCTC search page, prints the action-space JSON for the
search widget, types a station name into From, and prints the action space
again to show the autocomplete overlay's option elements appearing. Never
submits the form.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from irctc_dom_agent.browser import launch_page
from irctc_dom_agent.dom_indexer import DomIndexer

START_URL = "https://www.irctc.co.in/nget/train-search"


def main() -> None:
    indexer = DomIndexer()
    with launch_page() as page:
        page.goto(START_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        print("=== snapshot before any interaction ===")
        space = indexer.build(page)
        print(json.dumps(space.to_dict(), indent=2))

        if space.blocking_dialog:
            print("\nA blocking dialog is present (e.g. the language selector).")
            print("This read-only script does not dismiss it; agent_loop.py handles that as part of the full workflow.")
            page.wait_for_timeout(3000)
            return

        from_el = next((e for e in space.elements if "from station" in (e.name or "").lower()), None)
        if from_el is None:
            print("\nCould not locate the From field by name; stopping.")
            return

        locator = page.locator(f'[data-agent-idx="{from_el.id}"]')
        locator.click()
        locator.fill("Chennai")
        page.wait_for_timeout(1500)

        print("\n=== snapshot after typing 'Chennai' into From (autocomplete overlay expected) ===")
        space_after = indexer.build(page)
        print(json.dumps(space_after.to_dict(), indent=2))

        page.wait_for_timeout(3000)


if __name__ == "__main__":
    main()
