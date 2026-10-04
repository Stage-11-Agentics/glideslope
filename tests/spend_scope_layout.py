"""Local Playwright check for the Spend token-scope layout at phone width.

The page must start on Everyone with complete token data and include an account scope
with an incomplete split. Build that synthetic fixture, then run:
  uv run --extra dev --with playwright python tests/spend_scope_layout.py /tmp/spend.html --scope Alpha

This is kept outside pytest discovery because it needs an installed browser binary.
It only opens the local HTML file supplied on the command line.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def installed_chromium() -> Path | None:
    cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    candidates = list(cache.glob(
        "chromium_headless_shell-*/chrome-headless-shell-mac-arm64/chrome-headless-shell"
    ))
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda path: int(path.parent.parent.name.rsplit("-", 1)[-1]),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("page", type=Path, help="built local Spend HTML page")
    parser.add_argument("--scope", default="Alpha", help="account scope with an incomplete token split")
    parser.add_argument("--width", type=int, default=390)
    parser.add_argument("--height", type=int, default=844)
    parser.add_argument("--following-selector", default="#c-spd")
    args = parser.parse_args()
    if not args.page.is_file():
        parser.error(f"page does not exist: {args.page}")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        parser.error(f"install Playwright to run this local UI check: {exc}")

    with sync_playwright() as playwright:
        executable = installed_chromium()
        options = {"headless": True}
        if executable is not None:
            options["executable_path"] = str(executable)
        browser = playwright.chromium.launch(**options)
        page = browser.new_page(viewport={"width": args.width, "height": args.height})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.goto(args.page.resolve().as_uri(), wait_until="load")
        page.wait_for_selector("#tok-scope button")
        page.wait_for_selector(args.following_selector)

        selected = page.locator('#tok-scope button[data-scope="Everyone"]')
        if selected.count() != 1:
            raise AssertionError("fixture must start with the Everyone token scope")
        note = page.locator(".tok-note-extra")
        before_visibility = note.evaluate("element => getComputedStyle(element).visibility")
        if before_visibility != "hidden":
            raise AssertionError(
                "fixture must hide the missing-split note on Everyone before the scope click"
            )
        before = page.locator(args.following_selector).evaluate(
            "element => element.getBoundingClientRect().top + window.scrollY"
        )
        target = page.get_by_role("button", name=args.scope, exact=True)
        if target.count() != 1:
            raise AssertionError(f"expected one token-scope button named {args.scope!r}")
        target.click()
        after_visibility = note.evaluate("element => getComputedStyle(element).visibility")
        if after_visibility != "visible":
            raise AssertionError("scope must show the missing-split note for this fixture")
        after = page.locator(args.following_selector).evaluate(
            "element => element.getBoundingClientRect().top + window.scrollY"
        )
        if abs(after - before) >= 0.5:
            raise AssertionError(f"following section moved: document Y {before:.2f} → {after:.2f}")
        scroll_width, client_width = page.evaluate(
            "[document.documentElement.scrollWidth, document.documentElement.clientWidth]"
        )
        if scroll_width != client_width:
            raise AssertionError(f"horizontal overflow: {scroll_width}px > {client_width}px")
        if errors:
            raise AssertionError(f"browser reported errors: {errors}")
        browser.close()

    print(json.dumps({
        "width": args.width,
        "scope": args.scope,
        "following_selector": args.following_selector,
        "note_visibility": [before_visibility, after_visibility],
        "document_y_before": round(before, 2),
        "document_y_after": round(after, 2),
        "horizontal_overflow": False,
        "console_errors": 0,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
