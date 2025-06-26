import base64
from datetime import datetime
from typing import Any, TypedDict, cast

import requests


class DailyPinResponse(TypedDict):
    pin: str
    pinId: str


class IglooClient:
    _TIMEOUT = 10_000

    def __init__(self, access_token: str):
        """
        Public constructor for IglooClient.

        Args:
            access_token (str): The OAuth2 access token.
        """
        self._access_token = access_token

    @staticmethod
    def from_client_credentials(*, client_id: str, client_secret: str) -> "IglooClient":
        """
        Static factory method to create an IglooClient using client credentials.

        Args:
            client_id (str): The OAuth client ID.
            client_secret (str): The OAuth client secret.

        Returns:
            IglooClient: An instance of IglooClient with a valid access token.
        """
        basic_auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers = {"Authorization": f"Basic {basic_auth}", "Content-Type": "application/x-www-form-urlencoded"}
        data = {
            "grant_type": "client_credentials",
            # NOTE(thomas): We get all the scopes here, which kinda violates least privilege but ok
            "scope": "igloohomeapi/algopin-hourly igloohomeapi/algopin-daily igloohomeapi/algopin-permanent igloohomeapi/algopin-onetime igloohomeapi/create-pin-bridge-proxied-job igloohomeapi/delete-pin-bridge-proxied-job igloohomeapi/lock-bridge-proxied-job igloohomeapi/unlock-bridge-proxied-job igloohomeapi/get-device-status-bridge-proxied-job igloohomeapi/get-battery-level-bridge-proxied-job igloohomeapi/get-activity-logs-bridge-proxied-job igloohomeapi/get-devices igloohomeapi/get-job-status",
        }
        response = requests.post(
            "https://auth.igloohome.co/oauth2/token", headers=headers, data=data, timeout=IglooClient._TIMEOUT
        )
        response.raise_for_status()
        access_token = response.json().get("access_token")
        return IglooClient(access_token)

    def unlock(self, *, lock_id: str, bridge_id: str) -> Any:
        """
        Sends an unlock command to the specified lock via the specified bridge.

        Args:
            lock_id (str): The ID of the lock to unlock.
            bridge_id (str): The ID of the bridge to use.

        Returns:
            dict: The JSON response from the API.

        Raises:
            requests.HTTPError: If the API request fails.
        """
        url = f"https://api.igloodeveloper.co/igloohome/devices/{lock_id}/jobs/bridges/{bridge_id}"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self._access_token}"}
        data = {"jobType": 2}
        response = requests.post(url, headers=headers, json=data, timeout=IglooClient._TIMEOUT)
        response.raise_for_status()
        return response.json()

    def create_daily_pin(
        self, *, lock_id: str, start_date: datetime, end_date: datetime, access_name: str
    ) -> DailyPinResponse:
        """
        Creates a daily PIN that is active for the specified date range.

        Args:
            lock_id (str): The ID of the lock to set the PIN for.
            start_date (datetime): The start date/time as a Python datetime object.
            end_date (datetime): The end date/time as a Python datetime object.
            access_name (str): A name/label for the PIN access.

        Returns:
            DailyPinResponse: The parsed response from the API.

        Raises:
            requests.HTTPError: If the API request fails.
        """
        url = f"https://api.igloodeveloper.co/igloohome/devices/{lock_id}/algopin/hourly"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self._access_token}"}
        data = {
            "variance": 1,
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "accessName": access_name,
        }
        response = requests.post(url, headers=headers, json=data, timeout=IglooClient._TIMEOUT)
        response.raise_for_status()
        return cast(DailyPinResponse, response.json())
