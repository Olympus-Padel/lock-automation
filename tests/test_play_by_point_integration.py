import os

import pytest

from lock_automation.play_by_point import PlayByPointClient, _parse_entry_codes

pytestmark = pytest.mark.skipif(
    not os.environ.get("PLAY_BY_POINT_USERNAME"),
    reason="PlayByPoint credentials not set",
)


def test_login_and_get_rules() -> None:
    username = os.environ["PLAY_BY_POINT_USERNAME"]
    password = os.environ["PLAY_BY_POINT_PASSWORD"]
    owner_id = os.environ["DENVER_PLAY_BY_POINT_OWNER"]

    client = PlayByPointClient.from_login(username=username, password=password)

    rules_payload = client._api_get(f"{PlayByPointClient.BASE_URL}/api/rules?owner={owner_id}&namespace=facility_rules")
    assert isinstance(rules_payload, list)

    # Verify we can parse the entry codes from the rules response
    entry_codes = _parse_entry_codes(rules_payload)
    assert entry_codes["rule_id"]
    assert entry_codes["variant_id"]
    assert len(entry_codes["day_ids"]) > 0
