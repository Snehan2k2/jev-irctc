from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import Page

# Confirmed live: each train result row nests under a ".train-heading"
# element (train name + number), and departure/arrival/duration/available
# classes are all plain visible text within that same row - no fare is
# shown until a per-class "Refresh" link is clicked, so price isn't
# extracted here (yet).
_EXTRACT_JS = r"""
() => {
  const headings = Array.from(document.querySelectorAll('.train-heading'));
  return headings.map(h => {
    const row = h.closest('.ng-star-inserted') || h.parentElement.parentElement;
    const name = h.textContent.replace(/\s+/g, ' ').trim();
    const depEl = row.querySelector('.col-xs-5 .time strong, .col-xs-5 .time');
    const arrEl = row.querySelector('.col-xs-7 .pull-right.time, .col-xs-7 .time');
    const durEl = row.querySelector('.line-hr span, .line-hr');
    const classEls = Array.from(row.querySelectorAll('.pre-avl strong'));
    return {
      name,
      departure: depEl ? depEl.textContent.trim() : null,
      arrival: arrEl ? arrEl.textContent.trim() : null,
      duration: durEl ? durEl.textContent.trim() : null,
      classes: classEls.map(c => c.textContent.trim()),
    };
  });
}
"""

_NAME_RE = re.compile(r"^(.*?)\s*\((\d+)\)\s*$")
_DURATION_RE = re.compile(r"^(\d{1,3}):(\d{2})$")
_CLASS_CODE_RE = re.compile(r"\(([0-9A-Z]+)\)\s*$")


@dataclass
class TrainResult:
    name: str
    number: str
    departure: str
    arrival: str
    duration_minutes: int
    duration_text: str
    classes: list[str]


def _parse_duration_minutes(duration_text: Optional[str]) -> Optional[int]:
    if not duration_text:
        return None
    match = _DURATION_RE.match(duration_text.strip())
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _class_code(class_label: str) -> str:
    """"AC 3 Tier (3A)" -> "3A" - matches the class codes used in goal
    parsing (state.py's _CLASS_ALIASES canonical values)."""
    match = _CLASS_CODE_RE.search(class_label)
    return match.group(1) if match else class_label.strip().upper()


def extract_results(page: Page) -> list[TrainResult]:
    """Deterministic, code-only extraction of the already-rendered results
    list - no model call. This is plain data on the page; ranking it is
    sorting, not judgment, so there's nothing here for Jev to decide.
    """
    raw = page.evaluate(_EXTRACT_JS)
    results = []
    for item in raw:
        name_match = _NAME_RE.match(item["name"] or "")
        name = name_match.group(1).strip() if name_match else (item["name"] or "").strip()
        number = name_match.group(2) if name_match else ""
        duration_minutes = _parse_duration_minutes(item["duration"])
        if duration_minutes is None:
            continue  # can't rank a row whose duration didn't parse
        results.append(
            TrainResult(
                name=name,
                number=number,
                departure=(item["departure"] or "").strip(" |"),
                arrival=(item["arrival"] or "").strip(" |"),
                duration_minutes=duration_minutes,
                duration_text=item["duration"],
                classes=item["classes"],
            )
        )
    return results


def filter_by_class(results: list[TrainResult], target_class: Optional[str]) -> list[TrainResult]:
    if not target_class:
        return results
    target = target_class.strip().upper()
    return [r for r in results if target in (_class_code(c) for c in r.classes)]


def _departure_minutes(departure_text: str) -> Optional[int]:
    match = re.match(r"^(\d{1,2}):(\d{2})", (departure_text or "").strip())
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _circular_time_distance(a: int, b: int) -> int:
    """Minutes between two times-of-day, wrapping at midnight (so 23:45 and
    00:15 are 30 minutes apart, not ~23.5 hours)."""
    diff = abs(a - b)
    return min(diff, 24 * 60 - diff)


def rank_candidates(results: list[TrainResult], preferred_minutes: Optional[int]) -> list[TrainResult]:
    """Priority order: (1) closeness to the preferred departure time, (2)
    duration as the tiebreaker. No trains are dropped for being "far" from
    the preferred time - it's a ranking key, not a filter, so there's
    always a full ordering to check availability against. With no
    preference given, this is just a duration sort (the original
    behavior)."""
    if preferred_minutes is None:
        return sorted(results, key=lambda r: r.duration_minutes)

    def sort_key(r: TrainResult) -> tuple[int, int]:
        dep = _departure_minutes(r.departure)
        time_distance = _circular_time_distance(dep, preferred_minutes) if dep is not None else 24 * 60
        return (time_distance, r.duration_minutes)

    return sorted(results, key=sort_key)


# --- Live seat availability (one "Refresh" click per call - budget these) ---

MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_REFRESH_CLICK_JS = r"""
({trainNumber, classFragment}) => {
  const headings = Array.from(document.querySelectorAll('.train-heading'));
  const heading = headings.find(h => h.textContent.includes('(' + trainNumber + ')'));
  if (!heading) return { error: 'train row not found' };
  const row = heading.closest('.ng-star-inserted') || heading.parentElement.parentElement;
  const cells = Array.from(row.querySelectorAll('.pre-avl'));
  const cell = cells.find(c => c.textContent.includes(classFragment));
  if (!cell) return { error: 'class cell not found', seen: cells.map(c => c.textContent.trim()) };
  const link = cell.querySelector('.link, a, [class*="link" i]');
  if (!link) return { error: 'no refresh link in class cell (already refreshed?)' };
  link.click();
  return { clicked: true };
}
"""

_READ_AVAILABILITY_JS = r"""
(trainNumber) => {
  const headings = Array.from(document.querySelectorAll('.train-heading'));
  const heading = headings.find(h => h.textContent.includes('(' + trainNumber + ')'));
  if (!heading) return { error: 'train row not found' };
  const row = heading.closest('.ng-star-inserted') || heading.parentElement.parentElement;
  const cells = Array.from(row.querySelectorAll('.pre-avl'));
  if (!cells.length) return { error: 'no availability cells yet' };
  const first = cells[0];
  const dateEl = first.querySelector('strong');
  return {
    date_label: dateEl ? dateEl.textContent.trim() : null,
    text: first.textContent.replace(/\s+/g, ' ').trim(),
  };
}
"""

# Confirmed live: "AVAILABLE-0064", "WL43", "RAC 8" (note the space, unlike
# WL), "REGRET". Checked in this order since e.g. "AVAILABLE" never
# contains "WL"/"RAC" so order mostly doesn't matter, but keeping REGRET
# last is deliberate: it's a bare word with no digits, so it can't
# accidentally match inside a longer status.
_STATUS_PATTERNS = [
    (re.compile(r"AVAILABLE-?\s*0*(\d+)", re.I), "AVAILABLE"),
    (re.compile(r"\bRAC\s*\d+", re.I), "RAC"),
    (re.compile(r"\bWL\s*\d+", re.I), "WAITLIST"),
    (re.compile(r"\bREGRET\b", re.I), "NOT_AVAILABLE"),
]


@dataclass
class AvailabilityInfo:
    status: str  # "AVAILABLE" | "RAC" | "WAITLIST" | "NOT_AVAILABLE" | "UNKNOWN"
    detail: str
    seats: Optional[int] = None
    date_label: Optional[str] = None
    date_matches_target: bool = False


def _classify_status(text: str) -> tuple[str, Optional[int]]:
    for pattern, category in _STATUS_PATTERNS:
        match = pattern.search(text or "")
        if match:
            if category == "AVAILABLE":
                try:
                    return category, int(match.group(1))
                except (IndexError, ValueError):
                    return category, None
            return category, None
    return "UNKNOWN", None


def check_availability(
    page: Page, train: TrainResult, target_class: str, target_day: int, target_month: int
) -> AvailabilityInfo:
    """Clicks the "Refresh" link for `target_class` on `train`'s row and
    reads back the resulting date-wise availability panel. One live
    request per call - callers should cap how many trains get checked.

    Only the FIRST date column is read, since that's the one the click
    targeted - but a train that doesn't run on the target date shows its
    *next* running date there instead (confirmed live: a weekly train
    skipped straight to a date a week later), so the date label is checked
    against target_day/target_month before trusting the status; a mismatch
    is reported as "UNKNOWN" rather than silently misattributed.
    """
    click_result = page.evaluate(
        _REFRESH_CLICK_JS, {"trainNumber": train.number, "classFragment": f"({target_class})"}
    )
    if click_result.get("error"):
        return AvailabilityInfo(status="UNKNOWN", detail=click_result["error"])

    read_result: dict = {}
    for _ in range(6):
        page.wait_for_timeout(500)
        read_result = page.evaluate(_READ_AVAILABILITY_JS, train.number)
        if not read_result.get("error"):
            break
    if not read_result or read_result.get("error"):
        return AvailabilityInfo(status="UNKNOWN", detail=(read_result or {}).get("error", "no response"))

    date_label = read_result.get("date_label") or ""
    expected_month = MONTH_ABBR[target_month - 1]
    date_matches = str(target_day) in date_label and expected_month in date_label

    status, seats = _classify_status(read_result.get("text", ""))
    return AvailabilityInfo(
        status=status if date_matches else "UNKNOWN",
        detail=read_result.get("text", ""),
        seats=seats,
        date_label=date_label,
        date_matches_target=date_matches,
    )
