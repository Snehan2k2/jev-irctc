from __future__ import annotations

from playwright.sync_api import Page

from . import decision_model, executor, results, safety
from .browser import launch_page
from .dom_indexer import DomIndexer
from .reasoning_model import ReasoningUnavailable, resolve_ambiguous_element
from .state import Decision, State, WorkflowStep, parse_goal, parse_slot_date

DEFAULT_START_URL = "https://www.irctc.co.in/nget/train-search"

STEP_ORDER = [
    WorkflowStep.FILL_FROM,
    WorkflowStep.CONFIRM_FROM_SUGGESTION,
    WorkflowStep.FILL_TO,
    WorkflowStep.CONFIRM_TO_SUGGESTION,
    WorkflowStep.FILL_DATE,
    WorkflowStep.SELECT_DATE,
    WorkflowStep.SEARCH,
    WorkflowStep.IDENTIFY_TRAIN,
]

MAX_ITERATIONS = 60


def _next_step(step: WorkflowStep) -> WorkflowStep:
    if step not in STEP_ORDER:
        return WorkflowStep.DONE
    idx = STEP_ORDER.index(step)
    if idx + 1 < len(STEP_ORDER):
        return STEP_ORDER[idx + 1]
    return WorkflowStep.DONE


def human_confirm(decision: Decision, element_name: str) -> bool:
    answer = input(
        f"\n[CONFIRM REQUIRED] About to {decision.action} on {element_name!r}. "
        f"This looks like an irreversible step. Proceed? [y/N] "
    )
    return answer.strip().lower() in ("y", "yes")


def _run_iteration(page: Page, indexer: DomIndexer, state: State) -> bool:
    """Runs one loop tick. Returns False if the loop should stop."""
    action_space = indexer.build(page)
    state.current_url = action_space.url
    state.page_title = action_space.page_title

    trigger = safety.scan(page, action_space)
    if trigger:
        safety.explain_and_pause(trigger, state)
        return False

    if state.workflow_step == WorkflowStep.OPEN and not action_space.blocking_dialog:
        state.workflow_step = STEP_ORDER[0]

    # The calendar widget can take several ticks to resolve (navigate N
    # months, then click a day) - it auto-closes once a day is actually
    # selected, which is the reliable signal that SELECT_DATE is done and
    # it's safe to move on. Checked here, before decide(), so the same tick
    # that observes the closed calendar also proceeds to SEARCH instead of
    # wastefully calling decide() with calendar_open already False (which
    # would look like "nothing to do" and escalate to the reasoning layer).
    if state.workflow_step == WorkflowStep.SELECT_DATE and not action_space.calendar_open:
        state.workflow_step = WorkflowStep.SEARCH

    if state.workflow_step == WorkflowStep.IDENTIFY_TRAIN:
        # Results load asynchronously after the Search click - the very next
        # snapshot can still just be the search form itself (observed live:
        # Jev "identified" the Search button as the best-matching train
        # because no real result rows existed yet - a plain "does any
        # candidate contain a digit" check doesn't work either, since the
        # form page itself already has digit-bearing text like the date
        # value, the clock, and the copyright year). IRCTC's results view
        # replaces the full search widget with a condensed "Modify Search"
        # bar - confirmed live via screenshot - so use that as the marker.
        results_loaded = any(
            "modify search" in (e.name or "").lower() for e in action_space.elements
        )
        if not results_loaded:
            print("[IDENTIFY_TRAIN] results not loaded yet; waiting and rebuilding")
            page.wait_for_timeout(1000)
            return True

        # Deterministic, code-only from here - no model call. Departure,
        # arrival, duration, and available classes are all plain text
        # already on the page (confirmed live); filtering/ranking is just
        # code, nothing here needs Jev's judgment.
        all_results = results.extract_results(page)
        matching = results.filter_by_class(all_results, state.slots.travel_class)
        if not matching:
            print(
                f"[IDENTIFY_TRAIN] no results found offering class "
                f"{state.slots.travel_class!r} among {len(all_results)} trains"
            )
            state.result = {"top_available": [], "checked_count": 0, "matching_count": 0}
            state.workflow_step = WorkflowStep.DONE
            return False

        # Priority order: (1) closest to the preferred departure time, (2)
        # duration as the tiebreaker - a ranking, not a filter, so nothing
        # gets dropped for being "far" from the preferred time; with no
        # time preference given, this is a plain duration sort.
        preferred_minutes = state.slots.preferred_departure_minutes
        ranked = results.rank_candidates(matching, preferred_minutes)

        target = parse_slot_date(state.slots.date)
        target_day, target_month = (target[0], target[1]) if target else (None, None)

        MAX_CHECKS = 6
        TARGET_AVAILABLE = 3
        available: list[tuple] = []
        checked = 0
        if target_day is not None:
            for train in ranked:
                if checked >= MAX_CHECKS or len(available) >= TARGET_AVAILABLE:
                    break
                info = results.check_availability(page, train, state.slots.travel_class, target_day, target_month)
                checked += 1
                print(f"[IDENTIFY_TRAIN] checked {train.name} ({train.number}): {info.status} ({info.detail!r})")
                page.wait_for_timeout(400)
                if info.status == "AVAILABLE":
                    available.append((train, info))
        else:
            print("[IDENTIFY_TRAIN] no parseable target date; skipping live availability checks")

        state.result = {
            "requested_class": state.slots.travel_class,
            "preferred_departure_minutes": preferred_minutes,
            "matching_count": len(matching),
            "checked_count": checked,
            "top_available": [
                {
                    "name": r.name,
                    "number": r.number,
                    "departure": r.departure,
                    "arrival": r.arrival,
                    "duration": r.duration_text,
                    "seats": info.seats,
                    "availability": info.detail,
                }
                for r, info in available
            ],
        }
        state.workflow_step = WorkflowStep.DONE
        print(
            f"[DONE] {len(available)} confirmed-available train(s) found for {state.slots.travel_class} "
            f"(checked {checked} of {len(ranked)} candidates, priority: "
            f"{'closest departure time, then duration' if preferred_minutes is not None else 'duration only'}):"
        )
        for r, info in available:
            print(f"  {r.name} ({r.number}) - {r.departure} -> {r.arrival}, duration {r.duration_text}, {info.detail}")
        return False

    decision = decision_model.decide(state, action_space)
    if decision is None:
        context = decision_model.escalation_context(state, action_space)
        try:
            decision = resolve_ambiguous_element(state, action_space, context)
        except ReasoningUnavailable as exc:
            print(f"[PAUSED] No confident action and reasoning layer unavailable: {exc}")
            state.pause_reason = str(exc)
            state.workflow_step = WorkflowStep.PAUSED
            return False

    irreversible, matched_name = safety.is_irreversible(action_space, decision)
    if irreversible:
        element = action_space.get(decision.target)
        if not human_confirm(decision, element.name if element else matched_name):
            print("[STOPPED] User declined to confirm an irreversible action.")
            return False

    try:
        executor.perform(page, action_space, decision)
    except (executor.StaleSnapshotError, executor.ActionFailed) as exc:
        print(f"[REBUILD] {exc}")
        return True  # rebuild next tick, same workflow step

    print(f"[{state.workflow_step.name}] {decision.source}: {decision.action} -> {decision.target} ({decision.rationale})")

    # Settle delay: confirmed live that rebuilding immediately after a click
    # can still see the pre-action DOM mid-transition (e.g. the language
    # dialog's close animation is still in flight one tick after the click
    # that dismissed it), which would otherwise cause a false re-detection.
    page.wait_for_timeout(500)

    if action_space.blocking_dialog:
        return True  # still dismissing; don't advance the step yet

    if state.workflow_step == WorkflowStep.SELECT_DATE and action_space.calendar_open:
        return True  # still navigating/selecting within the calendar; don't advance yet

    if decision.action != "WAIT":
        state.workflow_step = _next_step(state.workflow_step)

    return True


def run(user_goal: str, start_url: str = DEFAULT_START_URL) -> State:
    state = State(user_goal=user_goal, slots=parse_goal(user_goal))
    indexer = DomIndexer()

    with launch_page() as page:
        page.goto(start_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        for _ in range(MAX_ITERATIONS):
            if not _run_iteration(page, indexer, state):
                break
        else:
            print(f"[STOPPED] Reached MAX_ITERATIONS ({MAX_ITERATIONS}) without finishing.")

        page.wait_for_timeout(2000)

    return state
