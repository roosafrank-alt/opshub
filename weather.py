"""Daily weather for the Flight School dashboard: current conditions and a
next-few-hours forecast for ZIP 12458 (Open-Meteo, free/no-key), civil
sunrise/sunset/twilight (sunrise-sunset.org, free/no-key), and a best-effort
aviation METAR + TAF for airport N89 (aviationweather.gov) - small
non-towered fields often don't report, so those two are allowed to come
back empty without failing anything else.

Only the standard library is used (urllib, json) so nothing extra needs to
be vendored onto the Pi - same reasoning as notify.py.

Results are cached in the weather_cache table for CACHE_TTL_SECONDS so the
dashboard doesn't make a live API call on every page load.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import db

LOCAL_TZ = "America/New_York"


def _utc_iso_to_local_12h(iso_str):
    """'2026-09-22T23:02:00+00:00' -> '7:02 PM' in LOCAL_TZ, or None."""
    if not iso_str:
        return None
    try:
        dt_utc = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(ZoneInfo(LOCAL_TZ))
        hour12 = dt_local.hour % 12 or 12
        return f"{hour12}:{dt_local.minute:02d} {'AM' if dt_local.hour < 12 else 'PM'}"
    except Exception:
        return None

CACHE_TTL_SECONDS = 20 * 60  # 20 minutes
ZIP_CODE = "12458"
N89_STATION = "N89"  # Joseph Y. Resnick Airport, Ellenville NY - the "bonus" aviation station
REQUEST_TIMEOUT = 8

# Fallback coordinates for ZIP 12458 (Palenville, NY / Catskill area), used
# only if the live ZIP lookup can't be reached - keeps the widget useful
# even on a flaky connection, at the cost of possibly-stale-if-ZIP-is-wrong
# coordinates.
FALLBACK_LAT = 42.19
FALLBACK_LON = -74.02

WMO_WEATHER_LABELS = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
    56: "Freezing drizzle", 57: "Dense freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Heavy freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Violent showers",
    85: "Light snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Severe thunderstorm w/ hail",
}


def _get_json(url, timeout=REQUEST_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "OpsHub-FlightSchool/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _zip_to_latlon(zip_code):
    """Zippopotam.us (free, no key) resolves a US ZIP to a lat/lon centroid."""
    try:
        data = _get_json(f"https://api.zippopotam.us/us/{urllib.parse.quote(zip_code)}")
        place = data["places"][0]
        return float(place["latitude"]), float(place["longitude"])
    except Exception:
        return FALLBACK_LAT, FALLBACK_LON


def _wx_category(code, cloud_cover_pct):
    """Buckets a WMO weather_code into a small set of categories the
    dashboard weather tile uses to pick its background gradient - clear /
    cloudy / fog / rain / snow / storm. cloud_cover_pct is the fallback when
    code itself is missing (unusual, but Open-Meteo can omit it)."""
    if code is None:
        if cloud_cover_pct is None:
            return "unknown"
        return "cloudy" if cloud_cover_pct >= 50 else "clear"
    if code in (0, 1):
        return "clear"
    if code in (2, 3):
        return "cloudy"
    if code in (45, 48):
        return "fog"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "rain"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (95, 96, 99):
        return "storm"
    return "cloudy"


def _fetch_conditions(lat, lon):
    """Open-Meteo current conditions: temp, wind, clouds, visibility, precip."""
    params = {
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,apparent_temperature,wind_speed_10m,wind_direction_10m,"
                   "wind_gusts_10m,cloud_cover,visibility,precipitation,weather_code,is_day",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        "precipitation_unit": "inch", "timezone": "auto",
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    data = _get_json(url)
    cur = data.get("current", {})
    code = cur.get("weather_code")
    cloud_cover_pct = cur.get("cloud_cover")
    return {
        "temp_f": cur.get("temperature_2m"),
        "feels_like_f": cur.get("apparent_temperature"),
        "wind_mph": cur.get("wind_speed_10m"),
        "wind_gusts_mph": cur.get("wind_gusts_10m"),
        "wind_dir_deg": cur.get("wind_direction_10m"),
        "cloud_cover_pct": cloud_cover_pct,
        "visibility_miles": (cur["visibility"] / 1609.34) if cur.get("visibility") is not None else None,
        "precipitation_in": cur.get("precipitation"),
        "weather_code": code,
        "weather_label": WMO_WEATHER_LABELS.get(code, "-"),
        "wx_category": _wx_category(code, cloud_cover_pct),
        "is_day": bool(cur.get("is_day", 1)),
        "observed_at": cur.get("time"),
    }


def _fetch_civil_twilight(lat, lon):
    """sunrise-sunset.org (free, no key) - civil dawn/dusk plus sunrise/sunset,
    returned in UTC; the API's date param defaults to "today" in UTC which is
    what we want here (approximate for a same-day dashboard widget)."""
    params = {"lat": lat, "lng": lon, "formatted": 0}
    url = "https://api.sunrise-sunset.org/json?" + urllib.parse.urlencode(params)
    data = _get_json(url)
    results = data.get("results", {})
    return {
        "sunrise_utc": results.get("sunrise"),
        "sunset_utc": results.get("sunset"),
        "civil_twilight_begin_utc": results.get("civil_twilight_begin"),
        "civil_twilight_end_utc": results.get("civil_twilight_end"),
        "sunrise_local": _utc_iso_to_local_12h(results.get("sunrise")),
        "sunset_local": _utc_iso_to_local_12h(results.get("sunset")),
        "civil_twilight_begin_local": _utc_iso_to_local_12h(results.get("civil_twilight_begin")),
        "civil_twilight_end_local": _utc_iso_to_local_12h(results.get("civil_twilight_end")),
    }


def _fetch_metar(station):
    """Best-effort raw METAR for a small GA field - aviationweather.gov has
    no data for a lot of non-towered airports, so a miss here is normal and
    should never blow up the rest of the weather widget."""
    try:
        params = {"ids": station, "format": "json"}
        url = "https://aviationweather.gov/api/data/metar?" + urllib.parse.urlencode(params)
        data = _get_json(url)
        if not data:
            return None
        row = data[0]
        return {
            "station": row.get("icaoId", station),
            "raw_text": row.get("rawOb"),
            "wind_dir_deg": row.get("wdir"),
            "wind_speed_kt": row.get("wspd"),
            "wind_gust_kt": row.get("wgst"),
            "visibility_sm": row.get("visib"),
            "temp_c": row.get("temp"),
            "observed_at": row.get("obsTime"),
        }
    except Exception:
        return None


def _fetch_taf(station):
    """Best-effort TAF (terminal forecast) for a station, same aviationweather.gov
    API and same "may come back empty" deal as _fetch_metar - most small
    non-towered fields don't have one."""
    try:
        params = {"ids": station, "format": "json"}
        url = "https://aviationweather.gov/api/data/taf?" + urllib.parse.urlencode(params)
        data = _get_json(url)
        if not data:
            return None
        row = data[0]
        return {
            "station": row.get("icaoId", station),
            "raw_text": row.get("rawTAF") or row.get("rawText"),
            "issued_at": row.get("issueTime"),
        }
    except Exception:
        return None


def _hour_12h_label(hour):
    """0-23 -> '1 PM' etc., for the dashboard's next-few-hours forecast strip."""
    hour12 = hour % 12 or 12
    return f"{hour12} {'AM' if hour < 12 else 'PM'}"


def _fetch_dashboard_forecast(lat, lon):
    """Next several hours' outlook (temp/wind/sky) for the dashboard strip -
    separate from get_forecast_for()'s single-hour lookup for one scheduled
    lesson, this always covers "the next few hours from now"."""
    now_local = datetime.now(ZoneInfo(LOCAL_TZ))
    hourly = _fetch_hourly_forecast(lat, lon, now_local.strftime("%Y-%m-%d"))
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m") or [None] * len(times)
    winds = hourly.get("wind_speed_10m") or [None] * len(times)
    gusts = hourly.get("wind_gusts_10m") or [None] * len(times)
    codes = hourly.get("weather_code") or [None] * len(times)
    out = []
    for i, t in enumerate(times):
        try:
            hour = int(t[11:13])
        except (ValueError, IndexError):
            continue
        if hour <= now_local.hour:
            continue
        out.append({
            "hour_label": _hour_12h_label(hour),
            "temp_f": temps[i],
            "wind_mph": winds[i],
            "wind_gusts_mph": gusts[i],
            "weather_code": codes[i],
            "weather_label": WMO_WEATHER_LABELS.get(codes[i], "-"),
            "wx_category": _wx_category(codes[i], None),
        })
        if len(out) >= 6:
            break
    return out


def _fetch_hourly_forecast(lat, lon, date_str):
    """Open-Meteo hourly forecast for one calendar date (YYYY-MM-DD) - used
    to show the forecast for a specific scheduled lesson's day/time. Only
    covers roughly the next 16 days (Open-Meteo's free forecast horizon);
    a date outside that range just comes back with no hours."""
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m,wind_speed_10m,wind_gusts_10m,wind_direction_10m,cloud_cover,"
                  "visibility,precipitation_probability,weather_code",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        "timezone": "auto", "start_date": date_str, "end_date": date_str,
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    data = _get_json(url)
    return data.get("hourly", {})


def get_forecast_for(conn, date_str, time_str=None, force=False):
    """Forecast conditions for one scheduled lesson's date + (optional)
    time - matched to the nearest available forecast hour. Cached per
    date+hour so re-clicking the same lesson doesn't re-hit the API."""
    hour = 12  # default to midday if no time was set on the booking
    if time_str:
        try:
            hour = int(time_str.split(":")[0])
        except Exception:
            pass
    cache_key = f"lesson_wx_{ZIP_CODE}_{date_str}_{hour:02d}"
    if not force:
        row = conn.execute(
            "SELECT payload, fetched_at FROM weather_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        if row:
            fetched_dt = None
            try:
                fetched_dt = datetime.strptime(row["fetched_at"], "%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
            # Forecasts change less often than "current conditions" - an hour-old
            # forecast cache entry is still fine, so this reuses CACHE_TTL_SECONDS
            # but is generous about it (a stale forecast is harmless either way).
            if fetched_dt and (datetime.now() - fetched_dt).total_seconds() <= CACHE_TTL_SECONDS * 3:
                try:
                    return json.loads(row["payload"])
                except Exception:
                    pass

    lat, lon = _zip_to_latlon(ZIP_CODE)
    payload = {"date": date_str, "hour": hour, "forecast": None, "error": None}
    try:
        hourly = _fetch_hourly_forecast(lat, lon, date_str)
        times = hourly.get("time", [])
        target = f"{date_str}T{hour:02d}:00"
        idx = times.index(target) if target in times else None
        if idx is None:
            payload["error"] = "No forecast available for that date yet (likely too far out)."
        else:
            code = (hourly.get("weather_code") or [None] * len(times))[idx]
            vis = (hourly.get("visibility") or [None] * len(times))[idx]
            payload["forecast"] = {
                "temp_f": (hourly.get("temperature_2m") or [None] * len(times))[idx],
                "wind_mph": (hourly.get("wind_speed_10m") or [None] * len(times))[idx],
                "wind_gusts_mph": (hourly.get("wind_gusts_10m") or [None] * len(times))[idx],
                "wind_dir_deg": (hourly.get("wind_direction_10m") or [None] * len(times))[idx],
                "cloud_cover_pct": (hourly.get("cloud_cover") or [None] * len(times))[idx],
                "visibility_miles": (vis / 1609.34) if vis is not None else None,
                "precip_probability_pct": (hourly.get("precipitation_probability") or [None] * len(times))[idx],
                "weather_code": code,
                "weather_label": WMO_WEATHER_LABELS.get(code, "-"),
            }
    except Exception as e:
        payload["error"] = f"Couldn't reach the weather service ({e.__class__.__name__})."

    try:
        conn.execute(
            "INSERT INTO weather_cache (cache_key, payload, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET payload = excluded.payload, fetched_at = excluded.fetched_at",
            (cache_key, json.dumps(payload), db.now_iso()))
        conn.commit()
    except Exception:
        pass

    return payload


def get_dashboard_weather(conn, force=False):
    """Cached wrapper: returns the combined weather payload, reusing the
    cache table when it's still fresh (within CACHE_TTL_SECONDS) so the
    dashboard doesn't hit three APIs on every load."""
    cache_key = f"dashboard_wx_{ZIP_CODE}"
    if not force:
        row = conn.execute(
            "SELECT payload, fetched_at FROM weather_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        if row:
            from datetime import datetime
            try:
                fetched = datetime.strptime(row["fetched_at"], "%Y-%m-%d %H:%M:%S")
                age = (datetime.now() - fetched).total_seconds()
            except Exception:
                age = CACHE_TTL_SECONDS + 1
            if 0 <= age <= CACHE_TTL_SECONDS:
                try:
                    return json.loads(row["payload"])
                except Exception:
                    pass  # fall through and refetch on a corrupt cache row

    lat, lon = _zip_to_latlon(ZIP_CODE)
    payload = {"zip": ZIP_CODE, "lat": lat, "lon": lon, "error": None}
    try:
        payload["conditions"] = _fetch_conditions(lat, lon)
    except Exception as e:
        payload["conditions"] = None
        payload["error"] = f"Couldn't reach the weather service ({e.__class__.__name__})."
    try:
        payload["twilight"] = _fetch_civil_twilight(lat, lon)
    except Exception:
        payload["twilight"] = None
    payload["metar"] = _fetch_metar(N89_STATION)  # None is a normal/expected outcome here
    payload["taf"] = _fetch_taf(N89_STATION)  # same deal - often empty for a small field
    try:
        payload["forecast"] = _fetch_dashboard_forecast(lat, lon)
    except Exception:
        payload["forecast"] = []

    try:
        conn.execute(
            "INSERT INTO weather_cache (cache_key, payload, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET payload = excluded.payload, fetched_at = excluded.fetched_at",
            (cache_key, json.dumps(payload), db.now_iso()))
        conn.commit()
    except Exception:
        pass  # caching is best-effort - a write failure shouldn't break the dashboard

    return payload
