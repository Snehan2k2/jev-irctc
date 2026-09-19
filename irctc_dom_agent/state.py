from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional

ACTIONS = (
    "CLICK",
    "TYPE_TEXT",
    "SELECT_OPTION",
    "CHECK",
    "UNCHECK",
    "PRESS_KEY",
    "SCROLL",
    "WAIT",
)


class WorkflowStep(Enum):
    OPEN = auto()
    FILL_FROM = auto()
    CONFIRM_FROM_SUGGESTION = auto()
    FILL_TO = auto()
    CONFIRM_TO_SUGGESTION = auto()
    FILL_DATE = auto()
    SELECT_DATE = auto()
    SEARCH = auto()
    IDENTIFY_TRAIN = auto()
    DONE = auto()
    PAUSED = auto()


@dataclass
class Decision:
    action: str
    snapshot_id: int
    target: Optional[int] = None
    text: Optional[str] = None
    option: Optional[str] = None
    key: Optional[str] = None
    seconds: Optional[float] = None
    confidence: float = 1.0
    source: str = "system1"  # "system1" | "system2" | "human"
    rationale: str = ""


@dataclass
class EscalationContext:
    """Scoped hand-off to the reasoning layer: the candidates and intent are
    narrowed by decision_model.py to what's actually relevant for the current
    step, not the whole page - handing Jev all ~60 elements on a page with a
    vague intent was observed live to make it pick an unrelated element (e.g.
    "Search Trains" when the task was picking a station suggestion).
    """

    candidate_ids: list[int]
    intent: str
    action: str = "CLICK"
    text: Optional[str] = None


@dataclass
class Slots:
    from_station: Optional[str] = None
    to_station: Optional[str] = None
    date: Optional[str] = None
    travel_class: Optional[str] = None
    preferred_departure_minutes: Optional[int] = None


@dataclass
class State:
    user_goal: str
    current_url: str = ""
    page_title: str = ""
    workflow_step: WorkflowStep = WorkflowStep.OPEN
    slots: Slots = field(default_factory=Slots)
    pause_reason: Optional[str] = None
    result: Optional[dict[str, Any]] = None


_CLASS_ALIASES = {
    "1a": "1A", "1ac": "1A", "first ac": "1A",
    "2a": "2A", "2ac": "2A", "second ac": "2A",
    "3a": "3A", "3ac": "3A", "third ac": "3A", "3e": "3E",
    "sl": "SL", "sleeper": "SL",
    "cc": "CC", "chair car": "CC",
    "2s": "2S", "second sitting": "2S",
    "ec": "EC", "executive": "EC",
}

_ROUTE_RE = re.compile(r"from\s+([a-z][a-z\s]*?)\s+to\s+([a-z][a-z\s]*?)(?:\s+on\b|\s+in\b|,|$)")
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b")

_TIME_AMPM_RE = re.compile(r"\b(\d{1,2})(?::([0-5]\d))?\s*(am|pm)\b", re.I)
_TIME_24H_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
# Checked longest-phrase-first so "late night"/"early morning" aren't
# shadowed by the shorter "night"/"morning" entries.
_PERIOD_WORDS = [
    ("late night", (23, 30)),
    ("early morning", (5, 0)),
    ("midnight", (0, 0)),
    ("morning", (8, 0)),
    ("noon", (12, 0)),
    ("afternoon", (14, 0)),
    ("evening", (18, 0)),
    ("night", (22, 0)),
]

MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def parse_slot_date(date_str: Optional[str]) -> Optional[tuple[int, int, int]]:
    """Parses the DD/MM/YYYY (or YYYY-MM-DD) string produced by parse_goal
    into (day, month, year). Shared by decision_model.py (calendar
    navigation) and agent_loop.py (availability-date verification)."""
    if not date_str:
        return None
    parts = re.split(r"[/-]", date_str.strip())
    if len(parts) != 3:
        return None
    try:
        if len(parts[0]) == 4:
            year, month, day = (int(p) for p in parts)
        else:
            day, month, year = (int(p) for p in parts)
    except ValueError:
        return None
    return day, month, year


def _parse_preferred_time(goal: str) -> Optional[int]:
    """Returns minutes-since-midnight for an explicit time ("6pm",
    "14:00") or a rough period word ("morning", "late night"), or None if
    the goal doesn't mention one. Deliberately simple - same "fixed,
    regex-based" approach as the rest of parse_goal, not a model call."""
    lowered = goal.lower()

    match = _TIME_AMPM_RE.search(lowered)
    if match:
        hour = int(match.group(1)) % 12
        minute = int(match.group(2) or 0)
        if match.group(3).lower() == "pm":
            hour += 12
        return hour * 60 + minute

    match = _TIME_24H_RE.search(goal)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))

    for phrase, (hour, minute) in _PERIOD_WORDS:
        if phrase in lowered:
            return hour * 60 + minute

    return None


def parse_goal(goal: str) -> Slots:
    """Deterministic slot extraction from a natural-language goal string.

    Deliberately simple regex/keyword matching, not a model call — the
    reasoning layer (Jev) is reserved for the two ambiguity-resolution cases
    described in the plan, not for parsing a fairly structured goal string.
    """
    lowered = goal.lower()
    slots = Slots()

    route = _ROUTE_RE.search(lowered)
    if route:
        slots.from_station = route.group(1).strip().title()
        slots.to_station = route.group(2).strip().title()

    date_match = _DATE_RE.search(goal)
    if date_match:
        slots.date = date_match.group(1)

    for alias, canonical in _CLASS_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            slots.travel_class = canonical
            break

    slots.preferred_departure_minutes = _parse_preferred_time(goal)

    return slots
