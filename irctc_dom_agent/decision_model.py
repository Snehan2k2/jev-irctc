from __future__ import annotations

import difflib
import re
from typing import Optional

from .action_space import TEXT_INPUT_ROLES, ActionSpace, ActionSpaceElement
from .state import Decision, EscalationContext, State, WorkflowStep, parse_slot_date

CONFIDENCE_THRESHOLD = 0.45
SUGGESTION_MATCH_THRESHOLD = 0.6
SUGGESTION_MARGIN = 0.15

FROM_KEYWORDS = ["from station", "enter from station", "from", "origin"]
TO_KEYWORDS = ["to station", "enter to station", "to", "destination"]
DATE_KEYWORDS = ["journey date", "date", "dd/mm/yyyy"]
SEARCH_KEYWORDS = ["search trains", "search", "find trains"]
DIALOG_DISMISS_KEYWORDS = ["english"]

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_CALENDAR_TITLE_RE = re.compile(r"^([A-Za-z]+)\s*(\d{4})$")


def _parse_calendar_title(title: Optional[str]) -> Optional[tuple[int, int]]:
    """IRCTC's PrimeNG calendar title renders as e.g. "September2026" (no
    space, confirmed live) - returns (month, year)."""
    if not title:
        return None
    match = _CALENDAR_TITLE_RE.match(title.strip())
    if not match:
        return None
    month_name, year_str = match.groups()
    month_name_l = month_name.strip().lower()
    for idx, name in enumerate(_MONTH_NAMES, start=1):
        if name.lower() == month_name_l:
            return idx, int(year_str)
    return None


def _score(name: str, keywords: list[str]) -> float:
    name_l = (name or "").lower()
    best = 0.0
    for kw in keywords:
        # Word-boundary match, not a raw substring check: a short keyword
        # like "to" was observed live to false-positive-match inside
        # unrelated text ("Input is manda-TO-ry"), misdirecting the TO field
        # onto the FROM field's label. \b keeps "to" from matching inside
        # "mandatory" while still matching it as a standalone word/phrase.
        if re.search(rf"\b{re.escape(kw)}\b", name_l):
            best = max(best, 0.9)
        else:
            best = max(best, difflib.SequenceMatcher(None, kw, name_l).ratio())
    return best


def _best_match(
    action_space: ActionSpace, keywords: list[str], roles: set[str]
) -> tuple[Optional[ActionSpaceElement], float]:
    best_el: Optional[ActionSpaceElement] = None
    best_score = 0.0
    for element in action_space.elements:
        if element.role not in roles:
            continue
        score = _score(element.name, keywords)
        if score > best_score:
            best_el, best_score = element, score
    return best_el, best_score


def _handle_blocking_dialog(action_space: ActionSpace) -> Optional[Decision]:
    element, score = _best_match(action_space, DIALOG_DISMISS_KEYWORDS, roles={"button"})
    if element and score >= 0.75:
        return Decision(
            action="CLICK",
            target=element.id,
            snapshot_id=action_space.snapshot_id,
            confidence=score,
            source="system1",
            rationale=f"dismissing blocking dialog via {element.name!r}",
        )
    return None  # ambiguous dialog -> let agent_loop escalate to the reasoning layer


def _fill_field(action_space: ActionSpace, keywords: list[str], value: Optional[str]) -> Optional[Decision]:
    if not value:
        return None
    element, score = _best_match(action_space, keywords, roles=TEXT_INPUT_ROLES)
    if element and score >= CONFIDENCE_THRESHOLD:
        return Decision(
            action="TYPE_TEXT",
            target=element.id,
            text=value,
            snapshot_id=action_space.snapshot_id,
            confidence=score,
            source="system1",
            rationale=f"matched field {element.name!r} for value {value!r}",
        )
    return None


def _click_field(action_space: ActionSpace, keywords: list[str]) -> Optional[Decision]:
    element, score = _best_match(action_space, keywords, roles={"button", "link"})
    if element and score >= CONFIDENCE_THRESHOLD:
        return Decision(
            action="CLICK",
            target=element.id,
            snapshot_id=action_space.snapshot_id,
            confidence=score,
            source="system1",
            rationale=f"matched {element.name!r}",
        )
    return None


def _pick_suggestion(action_space: ActionSpace, target_text: Optional[str]) -> Optional[Decision]:
    if not target_text:
        return None

    # IRCTC's own suggestion list includes a non-selectable divider entry
    # (observed live: "----- Stations -----") that still carries role="option" -
    # filter it out before scoring rather than let it pollute candidates.
    options = [e for e in action_space.elements if e.role == "option" and "---" not in (e.name or "")]
    if not options:
        # Suggestion overlay likely hasn't rendered yet (async autocomplete) -
        # wait and rebuild rather than escalating over an empty candidate set.
        return Decision(
            action="WAIT",
            seconds=0.6,
            snapshot_id=action_space.snapshot_id,
            confidence=1.0,
            source="system1",
            rationale="waiting for station suggestion list to render",
        )

    target = target_text.strip().lower()
    scored = []
    for element in options:
        name_l = (element.name or "").lower()
        score = 1.0 if target in name_l else difflib.SequenceMatcher(None, target, name_l).ratio()
        scored.append((score, element))
    scored.sort(key=lambda t: t[0], reverse=True)

    best_score, best_el = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    if best_score >= SUGGESTION_MATCH_THRESHOLD and (best_score - second_score) >= SUGGESTION_MARGIN:
        return Decision(
            action="CLICK",
            target=best_el.id,
            snapshot_id=action_space.snapshot_id,
            confidence=best_score,
            source="system1",
            rationale=f"best suggestion match for {target_text!r}: {best_el.name!r}",
        )
    return None  # genuinely ambiguous -> escalate to the reasoning layer


def _open_date_calendar(action_space: ActionSpace) -> Optional[Decision]:
    element, score = _best_match(action_space, DATE_KEYWORDS, roles=TEXT_INPUT_ROLES)
    if element and score >= CONFIDENCE_THRESHOLD:
        return Decision(
            action="CLICK",
            target=element.id,
            snapshot_id=action_space.snapshot_id,
            confidence=score,
            source="system1",
            rationale=f"opening the date calendar via {element.name!r}",
        )
    return None


def _select_date(action_space: ActionSpace, target_date_str: Optional[str]) -> Optional[Decision]:
    """IRCTC's journey-date field is a PrimeNG calendar widget, not a plain
    text input - typing into it does not register (confirmed live: the
    typed value was silently discarded and the field reverted to today's
    date). This navigates the open calendar to the target month/year via
    the Next/Previous Month links, then clicks the matching day cell.
    """
    if not action_space.calendar_open:
        return None

    target = parse_slot_date(target_date_str)
    current = _parse_calendar_title(action_space.calendar_title)
    if not target or not current:
        return None

    target_day, target_month, target_year = target
    current_month, current_year = current

    if (current_year, current_month) != (target_year, target_month):
        nav_name = "Next Month" if (current_year, current_month) < (target_year, target_month) else "Previous Month"
        element = next((e for e in action_space.elements if e.name == nav_name), None)
        if not element:
            return None
        return Decision(
            action="CLICK",
            target=element.id,
            snapshot_id=action_space.snapshot_id,
            confidence=0.95,
            source="system1",
            rationale=f"navigating calendar via {nav_name!r} toward {target_month:02d}/{target_year}",
        )

    # Enabled calendar days render as bare <a> tags (no href/tabindex,
    # confirmed live) - picked up via the cursor:pointer fallback scan and
    # classified role="link" by inferRole() since they're <a> elements.
    day_str = str(target_day)
    day_el = next(
        (e for e in action_space.elements if e.role == "link" and (e.name or "").strip() == day_str),
        None,
    )
    if not day_el:
        return None
    return Decision(
        action="CLICK",
        target=day_el.id,
        snapshot_id=action_space.snapshot_id,
        confidence=0.95,
        source="system1",
        rationale=f"selecting day {day_str!r} in the open calendar ({current_month:02d}/{current_year})",
    )


def decide(state: State, action_space: ActionSpace) -> Optional[Decision]:
    """System-1: pure heuristic matching, no model call.

    Returns None when nothing crosses the confidence bar - that's the
    explicit, only trigger for escalating to reasoning_model.py.
    """
    if action_space.blocking_dialog:
        return _handle_blocking_dialog(action_space)

    step = state.workflow_step
    if step == WorkflowStep.FILL_FROM:
        return _fill_field(action_space, FROM_KEYWORDS, state.slots.from_station)
    if step == WorkflowStep.CONFIRM_FROM_SUGGESTION:
        return _pick_suggestion(action_space, state.slots.from_station)
    if step == WorkflowStep.FILL_TO:
        return _fill_field(action_space, TO_KEYWORDS, state.slots.to_station)
    if step == WorkflowStep.CONFIRM_TO_SUGGESTION:
        return _pick_suggestion(action_space, state.slots.to_station)
    if step == WorkflowStep.FILL_DATE:
        return _open_date_calendar(action_space)
    if step == WorkflowStep.SELECT_DATE:
        return _select_date(action_space, state.slots.date)
    if step == WorkflowStep.SEARCH:
        return _click_field(action_space, SEARCH_KEYWORDS)

    # OPEN / IDENTIFY_TRAIN / DONE / PAUSED are handled directly by agent_loop.
    return None


def escalation_context(state: State, action_space: ActionSpace) -> EscalationContext:
    """Called by agent_loop only when decide() returned None. Scopes the
    candidates and intent to what's actually relevant for the current step -
    see EscalationContext's docstring for why that scoping matters.
    """
    if action_space.blocking_dialog:
        candidates = [e.id for e in action_space.elements if e.role == "button"]
        return EscalationContext(
            candidates,
            "dismiss the blocking modal dialog (e.g. choose a language) so the train search form becomes usable",
        )

    step = state.workflow_step

    if step in (WorkflowStep.CONFIRM_FROM_SUGGESTION, WorkflowStep.CONFIRM_TO_SUGGESTION):
        is_from = step == WorkflowStep.CONFIRM_FROM_SUGGESTION
        target_text = state.slots.from_station if is_from else state.slots.to_station
        which = "FROM (origin)" if is_from else "TO (destination)"
        candidates = [e.id for e in action_space.elements if e.role == "option" and "---" not in (e.name or "")]
        return EscalationContext(
            candidates,
            f"The user's stated {which} station is {target_text!r} (no specific station code given). "
            f"Select the suggestion entry that most plausibly corresponds to the main/central train "
            f"station for {target_text!r}, preferring the primary city station over suburban ones if "
            f"more than one matches.",
        )

    if step == WorkflowStep.FILL_FROM:
        candidates = [e.id for e in action_space.elements if e.role in TEXT_INPUT_ROLES]
        return EscalationContext(
            candidates,
            "select the input field for entering the FROM (origin) station",
            action="TYPE_TEXT",
            text=state.slots.from_station,
        )
    if step == WorkflowStep.FILL_TO:
        candidates = [e.id for e in action_space.elements if e.role in TEXT_INPUT_ROLES]
        return EscalationContext(
            candidates,
            "select the input field for entering the TO (destination) station",
            action="TYPE_TEXT",
            text=state.slots.to_station,
        )
    if step == WorkflowStep.FILL_DATE:
        candidates = [e.id for e in action_space.elements if e.role in TEXT_INPUT_ROLES]
        return EscalationContext(
            candidates,
            "select the input/control that opens the journey-date calendar picker "
            "(it is a calendar widget, not a plain text field - clicking it opens a date picker)",
        )
    if step == WorkflowStep.SELECT_DATE:
        candidates = [
            e.id
            for e in action_space.elements
            if e.name in ("Next Month", "Previous Month")
            or (e.role == "link" and (e.name or "").strip().isdigit())
        ]
        return EscalationContext(
            candidates,
            f"The open calendar is currently showing {action_space.calendar_title!r}. The user wants "
            f"the journey date {state.slots.date!r} (DD/MM/YYYY). Click 'Next Month' or 'Previous Month' "
            f"to navigate there if the shown month/year doesn't match, otherwise click the day-number "
            f"cell matching the target day.",
        )
    if step == WorkflowStep.SEARCH:
        candidates = [e.id for e in action_space.elements if e.role in ("button", "link")]
        return EscalationContext(candidates, "select the control that submits the train search")

    return EscalationContext(
        [e.id for e in action_space.elements],
        f"advance the current step ({step.name}) toward the user's goal: {state.user_goal}",
    )
