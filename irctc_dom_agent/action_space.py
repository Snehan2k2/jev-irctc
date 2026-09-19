from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# IRCTC's own From/To fields carry an explicit role="searchbox" rather than
# textbox (confirmed by running the indexer against the live page) - treated
# as text-input-capable everywhere a plain "textbox" role would be.
TEXT_INPUT_ROLES = {"textbox", "searchbox"}


def allowed_actions_for_role(role: str, tag: str) -> list[str]:
    """Per-role action gate. SELECT_OPTION is restricted to real <select> tags
    since Playwright's select_option() has no equivalent for ARIA-only
    combobox/listbox widgets — those get CLICK on their individual options
    once open instead.
    """
    if role == "checkbox":
        return ["CHECK", "UNCHECK", "CLICK"]
    if role == "radio":
        return ["CLICK"]
    if role in ("combobox", "listbox"):
        if tag == "select":
            return ["SELECT_OPTION", "CLICK"]
        return ["CLICK"]
    if role in TEXT_INPUT_ROLES:
        return ["TYPE_TEXT", "CLICK", "PRESS_KEY"]
    if role in ("button", "link", "tab", "option"):
        return ["CLICK"]
    return ["CLICK"]


@dataclass
class ActionSpaceElement:
    id: int
    role: str
    tag: str
    name: str
    name_quality: str
    value: Optional[str] = None
    checked: Optional[bool] = None
    options: Optional[list[str]] = None
    input_type: Optional[str] = None
    allowed_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "name_quality": self.name_quality,
            "allowed_actions": self.allowed_actions,
        }
        if self.value is not None:
            d["value"] = self.value
        if self.checked is not None:
            d["checked"] = self.checked
        if self.options:
            d["options"] = self.options
        return d


@dataclass
class ActionSpace:
    snapshot_id: int
    url: str
    page_title: str
    elements: list[ActionSpaceElement]
    blocking_dialog: bool = False
    calendar_open: bool = False
    calendar_title: Optional[str] = None

    def get(self, target_id: Optional[int]) -> Optional[ActionSpaceElement]:
        if target_id is None:
            return None
        for element in self.elements:
            if element.id == target_id:
                return element
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "url": self.url,
            "page_title": self.page_title,
            "blocking_dialog": self.blocking_dialog,
            "elements": [e.to_dict() for e in self.elements],
        }
