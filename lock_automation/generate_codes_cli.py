import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .igloo import IglooClient
from .play_by_point import PlayByPointClient


def setup_logging() -> logging.Logger:
    """Set up logging configuration."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger(__name__)


def create_pin_for_day(igloo: IglooClient, lock_id: str, day: date, tzinfo: ZoneInfo) -> str:
    start_date = datetime.combine(day, datetime.min.time(), tzinfo=tzinfo)
    end_date = start_date + timedelta(days=1)
    daily_pin = igloo.create_daily_pin(
        lock_id=lock_id,
        start_date=start_date,
        end_date=end_date,
        access_name=f"Pin for {start_date.date()}",
    )
    if daily_pin is None:
        raise ValueError(f"Failed to create daily pin for {day.isoformat()}")
    return daily_pin["pin"]


def main() -> None:
    logger = setup_logging()

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
    updated_codes: dict[str, str | None] = {}

    logger.info(f"Generating new lock pins for the next {args.num_days} days (starting tomorrow):")
    for i in range(1, args.num_days + 1):
        day = today + timedelta(days=i)
        logger.info(f"Generating pin for {day.isoformat()}...")
        try:
            code = create_pin_for_day(igloo=igloo, lock_id=args.igloo_lock_id, day=day, tzinfo=tz)
            updated_codes[str(day.day)] = code
            logger.info(f"Successfully generated pin for {day.isoformat()}")
        except Exception:
            logger.exception(f"Failed to generate pin for {day.isoformat()}")
            continue

    logger.info("Logging in to Playbypoint...")
    try:
        play_by_point = PlayByPointClient.from_login(
            username=args.play_by_point_username, password=args.play_by_point_password
        )
        logger.info("Successfully logged in to Playbypoint")
    except Exception:
        logger.exception("Failed to log in to Playbypoint")
        sys.exit(1)

    logger.info("Updating Playbypoint entry codes...")
    try:
        updated_codes["12"] = None
        play_by_point.update_entry_codes(owner_id=args.play_by_point_owner, codes=updated_codes)
        logger.info("Successfully updated Playbypoint entry codes")
    except Exception:
        logger.exception("Failed to update Playbypoint entry codes")
        sys.exit(1)


if __name__ == "__main__":
    main()
