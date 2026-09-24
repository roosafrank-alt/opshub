"""Sunrise/sunset and civil twilight for the flight dashboard header.

Worked out locally (the standard NOAA / "Almanac for Computers" sunrise
equation, good to about a minute), so it needs no internet, works for every
login (not just the CFI weather card) and always gives the LOCAL calendar
day's times - unlike the weather card's sunrise-sunset.org call, whose
"today" is the UTC day and flips to tomorrow after 8 PM Eastern.
"""
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Joseph Y. Resnick Airport (N89), Ellenville NY - the school's home field.
HOME_LAT = 41.7279
HOME_LON = -74.3774
LOCAL_TZ = "America/New_York"

ZENITH_OFFICIAL = 90.833  # sunrise / sunset
ZENITH_CIVIL = 96.0       # civil twilight begins / ends


def _event_utc(day, lat, lon, zenith, rising):
    """UTC datetime of one sun event on `day`, or None (polar day/night)."""
    n = day.timetuple().tm_yday
    lng_hour = lon / 15.0
    t = n + ((6 if rising else 18) - lng_hour) / 24.0
    m = 0.9856 * t - 3.289
    l = (m + 1.916 * math.sin(math.radians(m)) + 0.020 * math.sin(math.radians(2 * m)) + 282.634) % 360
    ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(l)))) % 360
    ra = (ra + (math.floor(l / 90) * 90 - math.floor(ra / 90) * 90)) / 15.0
    sin_dec = 0.39782 * math.sin(math.radians(l))
    cos_dec = math.cos(math.asin(sin_dec))
    cos_h = ((math.cos(math.radians(zenith)) - sin_dec * math.sin(math.radians(lat)))
             / (cos_dec * math.cos(math.radians(lat))))
    if cos_h > 1 or cos_h < -1:
        return None
    h = (360 - math.degrees(math.acos(cos_h))) if rising else math.degrees(math.acos(cos_h))
    local_mean = h / 15.0 + ra - 0.06571 * t - 6.622
    ut = (local_mean - lng_hour) % 24
    base = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return base + timedelta(hours=ut)


def _label(dt_utc, tz):
    if dt_utc is None:
        return None
    local = dt_utc.astimezone(tz)
    return f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def _zulu(dt_utc):
    return dt_utc.strftime("%H%MZ") if dt_utc else None


def get_sun_times(day=None, lat=HOME_LAT, lon=HOME_LON, tz_name=LOCAL_TZ):
    """Today's (local calendar day) sun times at the home field, as
    '6:52 AM'-style local labels plus HHMMZ Zulu labels. Never raises."""
    try:
        tz = ZoneInfo(tz_name)
        day = day or datetime.now(tz).date()
        events = {
            "civil_begin": _event_utc(day, lat, lon, ZENITH_CIVIL, True),
            "sunrise": _event_utc(day, lat, lon, ZENITH_OFFICIAL, True),
            "sunset": _event_utc(day, lat, lon, ZENITH_OFFICIAL, False),
            "civil_end": _event_utc(day, lat, lon, ZENITH_CIVIL, False),
        }
        # The equation returns a UTC time-of-day; evening events in the
        # Americas fall on the next UTC date, so pin each one to the local
        # calendar day it belongs to.
        for key, dt in events.items():
            if dt is None:
                continue
            while dt.astimezone(tz).date() < day:
                dt += timedelta(days=1)
            while dt.astimezone(tz).date() > day:
                dt -= timedelta(days=1)
            events[key] = dt
        out = {"date": day.isoformat()}
        for key, dt in events.items():
            out[key] = _label(dt, tz)
            out[key + "_z"] = _zulu(dt)
        return out
    except Exception:
        return None
