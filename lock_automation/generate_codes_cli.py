import argparse
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .igloo import IglooClient
from .play_by_point import PlayByPointClient


def create_pin_for_day(igloo: IglooClient, lock_id: str, day: date, tzinfo: ZoneInfo) -> str:
    start_date = datetime.combine(day, datetime.min.time(), tzinfo=tzinfo)
    end_date = start_date + timedelta(days=1)
    daily_pin = igloo.create_daily_pin(
        lock_id=lock_id,
        start_date=start_date,
        end_date=end_date,
        access_name=f"Pin for {start_date.date()}",
    )
    return daily_pin["pin"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync Igloo daily pins to Playbypoint entry codes for the next 2 weeks."
    )
    parser.add_argument("--igloo-client-id", required=True, help="Igloo API client ID")
    parser.add_argument("--igloo-client-secret", required=True, help="Igloo API client secret")
    parser.add_argument("--igloo-lock-id", required=True, help="Igloo lock ID")
    parser.add_argument("--play-by-point-username", required=True, help="Playbypoint username (email)")
    parser.add_argument("--play-by-point-password", required=True, help="Playbypoint password")
    parser.add_argument("--play-by-point-owner", required=True, help="Playbypoint owner ID")
    parser.add_argument("--timezone", required=True, help="Timezone string, e.g. 'America/Denver'")
    parser.add_argument("--num-days", type=int, default=14, help="Number of days to generate codes for (default: 14)")

    args = parser.parse_args()

    igloo = IglooClient.from_client_credentials(client_id=args.igloo_client_id, client_secret=args.igloo_client_secret)
    tz = ZoneInfo(args.timezone)
    today = datetime.now(tz).date()
    updated_codes: dict[str, str] = {}

    print(f"Generating new lock pins for the next {args.num_days} days (starting tomorrow):")
    for i in range(1, args.num_days + 1):
        day = today + timedelta(days=i)
        print(f"  - Generating pin for {day.isoformat()}...", end="", flush=True)
        try:
            code = create_pin_for_day(igloo=igloo, lock_id=args.igloo_lock_id, day=day, tzinfo=tz)
            updated_codes[str(day.day)] = code
            print(" done.")
        except Exception as e:
            print(f" failed! ({e})", file=sys.stderr)
            continue

    print("\nLogging in to Playbypoint...", end="", flush=True)
    try:
        play_by_point = PlayByPointClient.from_login(
            username=args.play_by_point_username, password=args.play_by_point_password
        )
        print(" success.")
    except Exception as e:
        print(f" failed! ({e})", file=sys.stderr)
        sys.exit(1)

    print("Updating Playbypoint entry codes...", end="", flush=True)
    try:
        play_by_point.update_entry_codes(owner_id=args.play_by_point_owner, codes=updated_codes)
        print(" success.")
    except Exception as e:
        print(f" failed! ({e})", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
