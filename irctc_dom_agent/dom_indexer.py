from __future__ import annotations

from playwright.sync_api import Page

from .action_space import ActionSpace, ActionSpaceElement, allowed_actions_for_role

# Walks the *whole visible document* (not a known container) because IRCTC's
# station-autocomplete and quota/class dropdowns render as overlay panels
# that are not DOM descendants of the field that triggered them. Every call
# re-stamps data-agent-idx from scratch; nothing from a previous snapshot is
# reused, so stale references can't leak across rebuilds.
_INDEX_JS = r"""
() => {
  const SELECTOR = [
    'input', 'textarea', 'select', 'button', 'a[href]',
    '[role="button"]', '[role="textbox"]', '[role="combobox"]',
    '[role="checkbox"]', '[role="radio"]', '[role="option"]',
    '[role="listbox"]', '[role="link"]', '[role="tab"]',
    '[tabindex]:not([tabindex="-1"])', '[contenteditable="true"]'
  ].join(',');

  function isVisible(el) {
    if (!(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function isEnabled(el) {
    if (el.disabled) return false;
    if (el.getAttribute('aria-disabled') === 'true') return false;
    // Component libraries (observed live: PrimeNG's calendar day cells)
    // often fake-disable via a CSS class rather than the disabled property.
    if (typeof el.className === 'string' && /\bdisabled\b/i.test(el.className)) return false;
    return true;
  }

  function labelFor(el) {
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl && lbl.textContent.trim()) return lbl.textContent.trim();
    }
    const wrapping = el.closest('label');
    if (wrapping && wrapping.textContent.trim()) return wrapping.textContent.trim();
    return null;
  }

  // Custom component wrappers (observed live: PrimeNG's
  // <p-calendar id="jDate"> around a plain, unlabeled native <input>) often
  // carry the id that <label for=...> points to, while the actual focusable
  // control nested inside has no id of its own. This must only run as a
  // last resort, AFTER an element's own text is checked - regression
  // observed live: applying it early made station-suggestion <li> items
  // (which sit inside the same "origin"-labeled wrapper as the From input)
  // report the ancestor's label "From" instead of their own visible station
  // name, since they don't carry an aria-label of their own either.
  function ancestorLabelFor(el) {
    let ancestor = el.parentElement;
    let depth = 0;
    while (ancestor && depth < 5) {
      if (ancestor.id) {
        const lbl = document.querySelector(`label[for="${CSS.escape(ancestor.id)}"]`);
        if (lbl && lbl.textContent.trim()) return lbl.textContent.trim();
      }
      ancestor = ancestor.parentElement;
      depth += 1;
    }
    return null;
  }

  function accessibleName(el) {
    const text = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');

    // PrimeNG's calendar prev/next arrows are icon-only <a> tags with no
    // text, aria-label, or id (observed live) - special-cased since there's
    // no generic accessible-name signal to fall back on for them at all.
    if (typeof el.className === 'string') {
      if (/\bui-datepicker-prev\b/.test(el.className)) return ['Previous Month', 'widget-specific'];
      if (/\bui-datepicker-next\b/.test(el.className)) return ['Next Month', 'widget-specific'];
    }

    const ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel && ariaLabel.trim()) {
      const trimmed = ariaLabel.trim();
      // IRCTC has buttons whose aria-label is a whole dialog's worth of
      // dumped text (observed live: two different buttons sharing one
      // 400+ char aria-label) - when that happens the short visible text
      // is the only thing that actually distinguishes the element.
      if (trimmed.length > 150 && text && text.length > 0 && text.length < trimmed.length) {
        return [text.slice(0, 120), 'text-over-verbose-aria-label'];
      }
      return [trimmed, 'aria-label'];
    }

    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const parts = labelledBy.split(/\s+/)
        .map(id => document.getElementById(id))
        .filter(Boolean)
        .map(n => n.textContent.trim())
        .filter(Boolean);
      if (parts.length) return [parts.join(' ').trim(), 'aria-labelledby'];
    }

    const lbl = labelFor(el);
    if (lbl) return [lbl, 'label'];

    const placeholder = el.getAttribute('placeholder');
    if (placeholder && placeholder.trim()) return [placeholder.trim(), 'placeholder'];

    if (text) return [text.slice(0, 120), 'text'];

    const title = el.getAttribute('title');
    if (title && title.trim()) return [title.trim(), 'title'];

    const fallback = el.getAttribute('formcontrolname') || el.getAttribute('name') || el.id;
    if (fallback) return [fallback, 'id-fallback'];

    const ancestorLbl = ancestorLabelFor(el);
    if (ancestorLbl) return [ancestorLbl, 'ancestor-label'];

    return ['', 'none'];
  }

  function inferRole(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'submit' || type === 'button') return 'button';
      return 'textbox';
    }
    if (el.isContentEditable) return 'textbox';
    return 'generic';
  }

  function currentValue(el, role, tag) {
    if (tag === 'select') {
      const opt = el.options[el.selectedIndex];
      return opt ? opt.textContent.trim() : '';
    }
    if (tag === 'input' || tag === 'textarea') {
      if (role === 'checkbox' || role === 'radio') return null;
      return el.value || '';
    }
    return null;
  }

  function optionsFor(el, tag) {
    if (tag === 'select') {
      return Array.from(el.options).map(o => o.textContent.trim());
    }
    return null;
  }

  document.querySelectorAll('[data-agent-idx]').forEach(el => el.removeAttribute('data-agent-idx'));

  const primary = new Set(document.querySelectorAll(SELECTOR));
  // Generic fallback for non-semantic clickables that carry no role,
  // href, or tabindex signal at all (observed live: PrimeNG calendar day
  // cells are <a> tags with neither href nor tabindex - only "today" gets
  // tabindex="0" - so only a "cursor: pointer" style marks them
  // interactive). Scoped to a few common tags rather than every element,
  // to keep the scan cheap. 'a' is included despite the primary selector
  // already covering a[href], specifically to catch href-less anchors.
  const EXTRA_SELECTOR = 'div, span, li, td, p, a';
  const extra = Array.from(document.querySelectorAll(EXTRA_SELECTOR)).filter(el => {
    if (primary.has(el)) return false;
    if (window.getComputedStyle(el).cursor !== 'pointer') return false;
    return true;
  });

  const nodes = [...primary, ...extra];
  const elements = [];
  let idx = 0;
  for (const el of nodes) {
    if (!isVisible(el) || !isEnabled(el)) continue;
    const tag = el.tagName.toLowerCase();
    const role = inferRole(el);
    const [name, nameQuality] = accessibleName(el);
    const id = idx;
    el.setAttribute('data-agent-idx', String(id));
    elements.push({
      id,
      role,
      tag,
      name,
      name_quality: nameQuality,
      value: currentValue(el, role, tag),
      checked: (role === 'checkbox' || role === 'radio') ? !!el.checked : null,
      options: optionsFor(el, tag),
      input_type: tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : null,
    });
    idx += 1;
  }

  const dialog = document.querySelector('[role="dialog"], [role="alertdialog"]');
  const blockingDialog = !!(dialog && isVisible(dialog));

  const calPanel = document.querySelector('.ui-datepicker');
  const calendarOpen = !!(calPanel && isVisible(calPanel));
  let calendarTitle = null;
  if (calendarOpen) {
    const titleEl = calPanel.querySelector('.ui-datepicker-title');
    calendarTitle = titleEl ? titleEl.textContent.trim() : null;
  }

  return { elements, blocking_dialog: blockingDialog, calendar_open: calendarOpen, calendar_title: calendarTitle };
}
"""


class DomIndexer:
    """Builds a fresh ActionSpace from the live DOM on every call.

    Never hands out a Playwright ElementHandle: targets are resolved lazily
    by executor.py via the data-agent-idx attribute stamped on this call,
    which is the only thing that survives between build() and perform().
    """

    def __init__(self) -> None:
        self._snapshot_id = 0

    def build(self, page: Page) -> ActionSpace:
        raw = page.evaluate(_INDEX_JS)
        elements = [
            ActionSpaceElement(
                id=item["id"],
                role=item["role"],
                tag=item["tag"],
                name=item["name"],
                name_quality=item["name_quality"],
                value=item.get("value"),
                checked=item.get("checked"),
                options=item.get("options"),
                input_type=item.get("input_type"),
                allowed_actions=allowed_actions_for_role(item["role"], item["tag"]),
            )
            for item in raw["elements"]
        ]
        self._snapshot_id += 1
        return ActionSpace(
            snapshot_id=self._snapshot_id,
            url=page.url,
            page_title=page.title(),
            elements=elements,
            blocking_dialog=raw["blocking_dialog"],
            calendar_open=raw["calendar_open"],
            calendar_title=raw["calendar_title"],
        )
