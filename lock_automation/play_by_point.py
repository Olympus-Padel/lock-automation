from collections.abc import Mapping
from typing import Any, TypedDict

import cloudscraper  # type: ignore[import-untyped]
import requests
from bs4 import BeautifulSoup


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
        # Use cloudscraper to handle Cloudflare protection
        session: requests.Session = cloudscraper.create_scraper()

        # Load sign-in page to capture CSRF token and cookies
        sign_in_page = session.get("https://app.playbypoint.com/users/sign_in")
        login_soup = BeautifulSoup(sign_in_page.text, "html.parser")
        login_csrf: str = login_soup.find("input", {"name": "authenticity_token"})["value"]  # type: ignore[index]

        payload = {"user[email]": username, "user[password]": password, "authenticity_token": login_csrf}

        # Submit POST to sign in
        sign_in_response = session.post(
            "https://app.playbypoint.com/users/sign_in",
            data=payload,
        )

        if "Incorrect" in sign_in_response.text or sign_in_response.status_code != 200:
            raise RuntimeError("Login failed")

        logged_in_soup = BeautifulSoup(sign_in_response.text, "html.parser")
        logged_in_csrf: str = logged_in_soup.find("meta", {"name": "csrf-token"})["content"]  # type: ignore[index]
        session.headers.update({"X-Csrf-Token": logged_in_csrf})

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
