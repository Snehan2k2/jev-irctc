from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from playwright.sync_api import Page, sync_playwright


@contextmanager
def launch_page(headless: bool = False) -> Iterator[Page]:
    """Plain Playwright Chromium session: no stealth patches, no UA/header
    spoofing, no fingerprint tricks. Headed by default so a human is present
    for anything safety.py pauses on.
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            page = browser.new_page()
            yield page
        finally:
            browser.close()
