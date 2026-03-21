import logging
import re
import time
from collections.abc import Mapping
from typing import Any, TypedDict

import requests

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


def _extract_csrf_token(html: str) -> str:
    """Extract the CSRF token from a Rails page's meta tag."""
    match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
    if not match:
        raise RuntimeError("Could not find CSRF token in page")
    return match.group(1)


def _extract_form_authenticity_token(html: str) -> str:
    """Extract the authenticity_token from a Rails form's hidden input."""
    match = re.search(r'<input[^>]*name="authenticity_token"[^>]*value="([^"]+)"', html)
    if not match:
        raise RuntimeError("Could not find authenticity_token in form")
    return match.group(1)


class PlayByPointClient:
    BASE_URL = "https://app.playbypoint.com"

    def __init__(self, session: requests.Session, csrf_token: str):
        self._session = session
        self._csrf_token = csrf_token

    @staticmethod
    def from_login(*, username: str, password: str, max_retries: int = 3) -> "PlayByPointClient":
        last_error: str = ""
        for attempt in range(1, max_retries + 1):
            # Use a fresh session each attempt to avoid stale cookies/Cloudflare state
            session = requests.Session()
            session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
                ),
            })

            # GET the sign-in page to obtain the form authenticity token and session cookie
            logger.info("Fetching login page...")
            resp = session.get(f"{PlayByPointClient.BASE_URL}/users/sign_in")
            if resp.status_code != 200:
                last_error = f"GET sign_in returned {resp.status_code}"
                logger.warning(f"Login attempt {attempt}: {last_error}")
                if attempt < max_retries:
                    time.sleep(2**attempt)
                continue

            form_token = _extract_form_authenticity_token(resp.text)

            # POST the login form
            logger.info("Submitting login form...")
            resp = session.post(
                f"{PlayByPointClient.BASE_URL}/users/sign_in",
                data={
                    "authenticity_token": form_token,
                    "user[email]": username,
                    "user[password]": password,
                    "user[remember_me]": "0",
                },
                allow_redirects=True,
            )

            if resp.status_code == 200 and "sign_in" not in resp.url:
                csrf_token = _extract_csrf_token(resp.text)
                logger.info("Login successful")
                return PlayByPointClient(session, csrf_token)

            last_error = f"POST sign_in returned {resp.status_code}, ended up at {resp.url}"
            logger.warning(f"Login attempt {attempt}: {last_error}")
            if attempt < max_retries:
                time.sleep(2**attempt)

        raise RuntimeError(f"Login failed after {max_retries} attempts — {last_error}")

    def _api_get(self, url: str) -> Any:
        """Make an authenticated GET request."""
        resp = self._session.get(url, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            raise RuntimeError(f"API request failed with status {resp.status_code}: {url}")
        return resp.json()

    def _api_put(self, url: str, data: dict[str, Any]) -> Any:
        """Make an authenticated PUT request."""
        resp = self._session.put(
            url,
            data=data,
            headers={
                "X-CSRF-Token": self._csrf_token,
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"API request failed with status {resp.status_code}: {url}")
        return resp.json() if resp.text else None

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
        rules_payload = self._api_get(f"{self.BASE_URL}/api/rules?owner={owner_id}&namespace=facility_rules")
        entry_codes = _parse_entry_codes(rules_payload)

        update_payload = _build_update_payload(owner_id=owner_id, entry_codes=entry_codes, updated_codes=codes)
        self._api_put(f"{self.BASE_URL}/api/rules/{entry_codes['rule_id']}", update_payload)
