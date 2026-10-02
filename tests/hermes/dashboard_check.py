"""Render the orchestration tab of the real Hermes Dashboard in headless Chromium (scripts/smoke-phase10.sh).

Runs in the Browser Runner image (Playwright) on the stack's network. Logs in through Hermes's own
password-login endpoint, opens the tab, walks the views, opens a task, approves a pending approval
from the page, and creates a task from the new-task form. Prints one JSON object with what was seen; writes a screenshot of the overview.

Usage: python3 dashboard_check.py <base url> <password file> <task key> <screenshot path>
"""

import json
import sys

from playwright.sync_api import sync_playwright

base, password_file, task, screenshot = sys.argv[1:5]
password = open(password_file, encoding="utf-8").read().strip()
seen = {}

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    login = page.request.post(f"{base}/auth/password-login",
                              data={"provider": "basic", "username": "operator", "password": password})
    seen["login"] = login.status
    page.goto(f"{base}/orchestration", wait_until="networkidle")
    page.get_by_text("Required actions").wait_for(timeout=30000)
    seen["overview"] = all(page.get_by_text(text).count() > 0 for text in ("Queue", "Running now", "Recent tests", "Platform"))
    # Hermes adds plugin tabs to its sidebar once their manifests load; the sidebar renders labels upper-case, so
    # the rendered text ORCHESTRATION appears only there (the page header keeps "Orchestration").
    page.wait_for_function("document.body.innerText.includes('ORCHESTRATION')", timeout=20000)
    seen["sidebar"] = True
    page.screenshot(path=screenshot, full_page=True)

    page.get_by_role("button", name="Board", exact=True).click()
    page.get_by_text(task, exact=True).first.wait_for(timeout=15000)
    seen["board"] = True
    page.get_by_text(task, exact=True).first.click()
    page.get_by_text("Audit timeline").wait_for(timeout=15000)
    seen["task_sections"] = [s for s in ("Plan", "Approvals", "Budget", "Quality Gate", "Reviews", "Tests", "Executions",
                                         "Manifests", "Audit timeline", "Checkpoints") if page.get_by_text(s).count() > 0]
    seen["timeline_rows"] = page.locator("text=TASK_CREATED").count()

    page.get_by_role("button", name="Projects", exact=True).click()
    page.get_by_text("Default branch").wait_for(timeout=15000)
    page.get_by_role("button", name="Workers", exact=True).click()
    page.get_by_text("Agent workers").wait_for(timeout=15000)
    page.get_by_role("button", name="Approvals", exact=True).click()
    approve = page.get_by_role("button", name="Approve", exact=True)
    approve.first.wait_for(timeout=15000)
    seen["pending_in_page"] = approve.count()
    approve.first.click()
    page.get_by_text("No decisions waiting.").wait_for(timeout=15000)
    seen["pending_after_approve"] = page.get_by_role("button", name="Approve", exact=True).count()

    # A new task from the form; a double click still creates one task (one idempotency key per form).
    page.get_by_role("button", name="+ New task", exact=True).click()
    page.get_by_text("What should be done").wait_for(timeout=15000)
    page.locator("form select").first.select_option("app")
    page.locator("form textarea").fill("Add a /version endpoint returning the package version")
    page.locator("form input").first.fill("Version endpoint")
    page.get_by_role("button", name="Create task", exact=True).dblclick()
    page.get_by_text("Audit timeline").wait_for(timeout=15000)
    seen["created_from_form"] = page.get_by_text("Version endpoint").count() > 0
    browser.close()
print(json.dumps(seen))
