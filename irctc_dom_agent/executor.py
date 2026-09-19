from __future__ import annotations

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .action_space import ActionSpace
from .state import Decision


class StaleSnapshotError(Exception):
    """A decision referenced a snapshot_id (or a target id within it) that no
    longer matches the live DOM. The caller must rebuild the action space and
    re-decide - never guess at where the target moved to.
    """


class ActionFailed(Exception):
    """A Playwright action was attempted on a still-present, correctly
    targeted element but failed anyway (observed live: a click on the real
    "Search Trains" button timed out because an unrelated overlay div was
    intercepting pointer events after an earlier misstep). Distinct from
    StaleSnapshotError - the target wasn't stale, the interaction itself
    failed - so callers should rebuild and let the loop re-decide rather than
    let the whole run crash.
    """


def perform(page: Page, action_space: ActionSpace, decision: Decision) -> None:
    if decision.snapshot_id != action_space.snapshot_id:
        raise StaleSnapshotError(
            f"Decision was made for snapshot {decision.snapshot_id} but the "
            f"current snapshot is {action_space.snapshot_id}."
        )

    if decision.action == "WAIT":
        page.wait_for_timeout(int((decision.seconds or 1.0) * 1000))
        return

    if decision.target is None:
        raise ValueError(f"Action {decision.action} requires a target element id.")

    locator = page.locator(f'[data-agent-idx="{decision.target}"]')
    if locator.count() == 0:
        raise StaleSnapshotError(
            f"Target {decision.target} is not present in the current DOM (stale)."
        )

    try:
        if decision.action == "CLICK":
            try:
                locator.click(timeout=8000)
            except PlaywrightTimeoutError:
                # Observed live on IRCTC: a sibling container (the
                # "form-swap" div) can grow after a validation state change
                # and end up intercepting pointer events on an otherwise
                # visible/enabled/stable button, purely as a CSS layout
                # quirk - not a security/anti-bot mechanism. Retry once with
                # a forced click before giving up.
                locator.click(timeout=8000, force=True)
        elif decision.action == "TYPE_TEXT":
            locator.fill(decision.text or "", timeout=8000)
        elif decision.action == "SELECT_OPTION":
            locator.select_option(label=decision.option, timeout=8000)
        elif decision.action == "CHECK":
            locator.check(timeout=8000)
        elif decision.action == "UNCHECK":
            locator.uncheck(timeout=8000)
        elif decision.action == "PRESS_KEY":
            locator.press(decision.key or "Enter", timeout=8000)
        elif decision.action == "SCROLL":
            locator.scroll_into_view_if_needed(timeout=8000)
        else:
            raise ValueError(f"Unknown action: {decision.action}")
    except PlaywrightTimeoutError as exc:
        raise ActionFailed(
            f"{decision.action} on target {decision.target} timed out (e.g. blocked by an "
            f"overlapping element): {exc}"
        ) from exc
