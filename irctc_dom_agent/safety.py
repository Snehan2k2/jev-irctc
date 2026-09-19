from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import Page

from .action_space import TEXT_INPUT_ROLES, ActionSpace
from .state import Decision, State, WorkflowStep

CAPTCHA_SELECTOR = (
    'img[src*="captcha" i], img[id*="captcha" i], img[class*="captcha" i], '
    'iframe[src*="recaptcha" i], iframe[src*="hcaptcha" i], '
    '[class*="captcha" i], [id*="captcha" i]'
)

OTP_PATTERN = re.compile(r"otp|one[\s-]?time password", re.I)
PAYMENT_PATTERN = re.compile(r"proceed to pay|card number|\bcvv\b|\bpay now\b", re.I)
# IRCTC's own nav/footer advertises payment products by name ("IRCTC-iPAY
# Payment Gateway", "Pay via UPI" links etc.) - matching those as a live
# payment step would be a false positive, so the payment scan below is
# restricted to roles that represent an active form/control, not navigation.
PAYMENT_TRIGGER_ROLES = {"button", "textbox", "searchbox"}
BOOKING_CONFIRM_PATTERN = re.compile(
    r"\bbook now\b|\bconfirm(?:\s+booking)?\b|\bpay now\b|\bproceed\b|place order", re.I
)


@dataclass
class SafetyTrigger:
    kind: str  # "captcha" | "otp" | "login" | "payment"
    detail: str
    element_id: Optional[int] = None


def _any_visible(page: Page, selector: str, limit: int = 5) -> bool:
    locator = page.locator(selector)
    count = min(locator.count(), limit)
    for i in range(count):
        try:
            if locator.nth(i).is_visible():
                return True
        except Exception:
            continue
    return False


def scan(page: Page, action_space: ActionSpace) -> Optional[SafetyTrigger]:
    """Checked every loop iteration before any decision is made. CAPTCHA
    widgets are scanned directly on the page (img/iframe are not
    "actionable" elements so they never reach the action space); OTP/login
    are checked against the indexed elements since those are ordinary,
    visible inputs.
    """
    if _any_visible(page, CAPTCHA_SELECTOR):
        return SafetyTrigger("captcha", "A CAPTCHA-like image/iframe is visible on the page.")

    for element in action_space.elements:
        if element.role in TEXT_INPUT_ROLES and element.input_type == "password":
            return SafetyTrigger("login", f"A password field is visible: {element.name!r}", element.id)
        haystack = element.name or ""
        if element.role in TEXT_INPUT_ROLES and OTP_PATTERN.search(haystack):
            return SafetyTrigger("otp", f"Element suggests OTP entry: {element.name!r}", element.id)
        if element.role in PAYMENT_TRIGGER_ROLES and PAYMENT_PATTERN.search(haystack):
            return SafetyTrigger("payment", f"Element suggests a payment step: {element.name!r}", element.id)

    if re.search(r"payment|checkout", page.url, re.I):
        return SafetyTrigger("payment", f"URL suggests a payment/checkout page: {page.url}")

    return None


def is_irreversible(action_space: ActionSpace, decision: Decision) -> tuple[bool, str]:
    if decision.action != "CLICK" or decision.target is None:
        return False, ""
    element = action_space.get(decision.target)
    if not element or not element.name:
        return False, ""
    if BOOKING_CONFIRM_PATTERN.search(element.name):
        return True, element.name
    return False, ""


def explain_and_pause(trigger: SafetyTrigger, state: State) -> None:
    print(f"\n[SAFETY PAUSE] Detected {trigger.kind}: {trigger.detail}")
    print("Stopping automated interaction here. Please handle this step yourself in the browser window.")
    state.pause_reason = f"{trigger.kind}: {trigger.detail}"
    state.workflow_step = WorkflowStep.PAUSED
