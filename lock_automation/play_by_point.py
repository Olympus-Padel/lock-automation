import json
import logging
from collections.abc import Mapping
from typing import Any, TypedDict

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

logger = logging.getLogger(__name__)


class ExistingEntryCode(TypedDict):
    id: int
    value: str


class EntryCodesResponse(TypedDict):
    rule_id: int
    variant_id: int
    day_ids: dict[str, int]
    existing_values: dict[int, ExistingEntryCode]  # variant_id -> ExistingEntryCode


def _parse_entry_codes(payload: list[dict[str, Any]]) -> EntryCodesResponse:
    for rule in payload:
        if rule["display_name"] != "Entry Access Codes":
            continue

        day_variant = next((v for v in rule.get("variants", []) if v.get("display_name") == "Day"), None)
        if not day_variant:
            raise ValueError("Couldn't find Day variant")

        day_ids = {v["text"]: v["value"] for v in day_variant.get("values", [])}

        existing_values: dict[int, ExistingEntryCode] = {}
        for val in rule.get("values", []):
            for variant in val.get("variants", []):
                variant_id = variant["rule_variant_item_id"]
                existing_values[variant_id] = {"id": val["id"], "value": val["value"]}

        return {
            "rule_id": rule["id"],
            "variant_id": day_variant["id"],
            "day_ids": day_ids,
            "existing_values": existing_values,
        }

    raise ValueError("Couldn't find EntryAccessCodes")


def _build_update_payload(
    *, owner_id: str, entry_codes: EntryCodesResponse, updated_codes: Mapping[str, str | None]
) -> dict[str, Any]:
    payload: dict[str, Any] = {"rule[id]": entry_codes["rule_id"], "owner": owner_id}

    for i, (day, variant_id) in enumerate(entry_codes["day_ids"].items()):
        if day in updated_codes:
            code = updated_codes[day]
            if code is None:
                # Omitting from the payload will clear it if it exists, or do nothing if it doesn't
                continue
        elif variant_id in entry_codes["existing_values"]:
            # Preserve existing code
            code = entry_codes["existing_values"][variant_id]["value"]
        else:
            # No update and no existing value
            continue

        prefix = f"rule[values_attributes][{i}]"

        if variant_id in entry_codes["existing_values"]:
            # We have to include these fields for an update
            existing = entry_codes["existing_values"][variant_id]
            payload[f"{prefix}[id]"] = existing["id"]

        # We always include these fields for a write
        payload[f"{prefix}[rule_id]"] = entry_codes["rule_id"]
        payload[f"{prefix}[value]"] = code
        payload[f"{prefix}[value_variants_attributes][0][variant_rule_id]"] = entry_codes["variant_id"]
        payload[f"{prefix}[value_variants_attributes][0][rule_variant_item_id]"] = str(variant_id)

    return payload


class PlayByPointClient:
    def __init__(self, playwright: Playwright, browser: Browser, context: BrowserContext, page: Page):
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self._page = page

    @staticmethod
    def from_login(*, username: str, password: str) -> "PlayByPointClient":
        # Use Playwright to handle Cloudflare protection
        # Note: We keep the browser open for subsequent API calls
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = context.new_page()

        # Hide webdriver property and other automation indicators
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            window.chrome = {runtime: {}};
        """)

        # Navigate to login page
        logger.info("Navigating to login page...")
        page.goto("https://app.playbypoint.com/users/sign_in", wait_until="domcontentloaded")

        # Wait for Cloudflare challenge to complete (if any) by waiting for login form
        # with retries, as Cloudflare may take time to verify
        logger.info("Waiting for login form (Cloudflare may take time)...")
        try:
            page.wait_for_selector('input[name="user[email]"]', timeout=60000)
        except Exception as e:
            # Log page state for debugging
            logger.exception(f"Login form not found. Page title: {page.title()}")
            logger.info(f"Page URL: {page.url}")
            logger.info(f"Page content preview: {page.content()[:2000]}")
            browser.close()
            playwright.stop()
            raise RuntimeError("Could not load login page - Cloudflare may be blocking") from e

        # Fill in login form
        logger.info("Filling login form...")
        page.fill('input[name="user[email]"]', username)
        page.fill('input[name="user[password]"]', password)
        page.click('input[type="submit"]')

        # Wait for navigation after login (URL should change away from sign_in)
        page.wait_for_url(lambda url: "sign_in" not in url, timeout=30000)

        # Check for login failure
        if "sign_in" in page.url or "Incorrect" in page.content():
            browser.close()
            playwright.stop()
            raise RuntimeError("Login failed")

        logger.info("Login successful")
        return PlayByPointClient(playwright, browser, context, page)

    def close(self) -> None:
        """Close the browser and clean up resources."""
        self._browser.close()
        self._playwright.stop()

    def _api_get(self, url: str) -> Any:
        """Make a GET request using the browser context."""
        result = self._page.evaluate(
            """async (url) => {
            const resp = await fetch(url);
            return {status: resp.status, body: await resp.text()};
        }""",
            url,
        )
        if result["status"] != 200:
            raise RuntimeError(f"API request failed with status {result['status']}: {url}")
        return json.loads(result["body"])

    def _api_put(self, url: str, data: dict[str, Any]) -> Any:
        """Make a PUT request using the browser context."""
        # Get CSRF token from meta tag
        csrf_token = self._page.locator('meta[name="csrf-token"]').get_attribute("content")

        result = self._page.evaluate(
            """async ({url, data, csrfToken}) => {
            const formData = new URLSearchParams();
            for (const [key, value] of Object.entries(data)) {
                formData.append(key, value);
            }
            const resp = await fetch(url, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'X-CSRF-Token': csrfToken,
                },
                body: formData.toString(),
            });
            return {status: resp.status, body: await resp.text()};
        }""",
            {"url": url, "data": data, "csrfToken": csrf_token},
        )
        if result["status"] != 200:
            raise RuntimeError(f"API request failed with status {result['status']}: {url}")
        return json.loads(result["body"]) if result["body"] else None

    def update_entry_codes(self, *, owner_id: str, codes: Mapping[str, str | None]) -> None:
        """
        Updates the Playbypoint facility settings with the provided daily codes.

        Providing None as the code value will clear the code for that day.
        Omitting a key results in no change.

        Args:
            owner_id (str): The ID of the owner.
            codes (dict): A mapping of days of the month (1-31) to the new codes.

        Raises:
            RuntimeError: If the API request fails.
        """
        rules_payload = self._api_get(
            f"https://app.playbypoint.com/api/rules?owner={owner_id}&namespace=facility_rules"
        )
        entry_codes = _parse_entry_codes(rules_payload)

        update_payload = _build_update_payload(owner_id=owner_id, entry_codes=entry_codes, updated_codes=codes)
        self._api_put(f"https://app.playbypoint.com/api/rules/{entry_codes['rule_id']}", update_payload)
