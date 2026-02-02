import logging
from collections.abc import Mapping
from typing import Any, TypedDict

import requests
from playwright.sync_api import sync_playwright

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
    def __init__(self, session: requests.Session):
        self._session = session

    @staticmethod
    def from_login(*, username: str, password: str) -> "PlayByPointClient":
        # Use Playwright to handle Cloudflare protection
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
            )
            page = context.new_page()

            # Hide webdriver property to avoid detection
            page.add_init_script('Object.defineProperty(navigator, "webdriver", {get: () => undefined});')

            # Navigate to login page
            logger.info("Navigating to login page...")
            page.goto("https://app.playbypoint.com/users/sign_in", wait_until="domcontentloaded")

            # Wait for login form to appear (may take time if Cloudflare challenge runs)
            page.wait_for_selector('input[name="user[email]"]', timeout=30000)

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
                raise RuntimeError("Login failed")

            # Extract CSRF token from meta tag
            csrf_token = page.locator('meta[name="csrf-token"]').get_attribute("content")
            if not csrf_token:
                browser.close()
                raise RuntimeError("Could not find CSRF token after login")

            # Transfer cookies to requests session
            cookies = context.cookies()
            browser.close()

        session = requests.Session()
        for cookie in cookies:
            session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"])
        session.headers.update({"X-Csrf-Token": csrf_token})

        logger.info("Login successful")
        return PlayByPointClient(session)

    def update_entry_codes(self, *, owner_id: str, codes: Mapping[str, str | None]) -> None:
        """
        Updates the Playbypoint facility settings with the provided daily codes.

        Providing None as the code value will clear the code for that day.
        Omitting a key results in no change.

        Args:
            owner_id (str): The ID of the owner.
            codes (dict): A mapping of days of the month (1-31) to the new codes.

        Raises:
            requests.HTTPError: If the API request fails.
        """
        entry_codes_resp = self._session.get(
            f"https://app.playbypoint.com/api/rules?owner={owner_id}&namespace=facility_rules"
        )
        entry_codes_resp.raise_for_status()
        rules_payload = entry_codes_resp.json()
        entry_codes = _parse_entry_codes(rules_payload)

        update_payload = _build_update_payload(owner_id=owner_id, entry_codes=entry_codes, updated_codes=codes)
        update_resp = self._session.put(
            f"https://app.playbypoint.com/api/rules/{entry_codes['rule_id']}", data=update_payload
        )
        update_resp.raise_for_status()
