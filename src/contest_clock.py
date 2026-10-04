"""Campus calendar calculations use UTC+08:00, independent of server timezone."""

from datetime import date, datetime, timedelta, timezone


CAMPUS_TIMEZONE = timezone(timedelta(hours=8), name="UTC+08:00")


def contest_now() -> datetime:
    return datetime.now(CAMPUS_TIMEZONE)


def calendar_date(current: date | datetime) -> date:
    if isinstance(current, datetime):
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("Current time requires an explicit UTC offset")
        return current.astimezone(CAMPUS_TIMEZONE).date()
    return current
