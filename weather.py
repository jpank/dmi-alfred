#!/usr/bin/env python3
"""
DMI Cyclist Weather — Alfred Script Filter
Fetches cyclist-focused weather and outputs Alfred JSON.
Uses Open-Meteo Geocoding API for city -> coordinates + location id.
Uses DMI Open Data API for nearby live observations in Denmark.
Uses DMI's public location endpoint for city forecasts and direct dmi.dk links.

Alfred setup:
  Script Filter -> Language: /bin/bash
  Script: python3 /path/to/weather.py "{query}"
  Connect Script Filter -> Open URL action (passes {query} arg through)
"""

import sys
import json
import math
import os
import urllib.request
import urllib.parse
import unicodedata
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Cache only Synop stations (v3 = larger station set + forecast fallback)
STATION_LIMIT = 2000
STATIONS_CACHE = "/tmp/dmi_synop_stations_v3.json"
CACHE_MAX_AGE_SECONDS = 86400  # 1 day

BASE_URL = "https://opendataapi.dmi.dk/v2/metObs/collections"
STATION_URL = f"{BASE_URL}/station/items?limit={STATION_LIMIT}"
OBS_URL = f"{BASE_URL}/observation/items"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
DMI_LOCATION_API_URL = "https://www.dmi.dk/NinJo2DmiDk/ninjo2dmidk"
DMI_LOCATION_PAGE_URL = "https://www.dmi.dk/lokation/show/{country_code}/{location_id}/{slug}/"

# Max distance for preferring live observations over forecast data
OBSERVATION_MAX_DISTANCE_KM = 75

COMPASS_DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
COMPASS_NAMES = {
    "N": "North", "NE": "Northeast", "E": "East", "SE": "Southeast",
    "S": "South", "SW": "Southwest", "W": "West", "NW": "Northwest",
}

# Query aliases — expanded before geocoding
ALIASES = {
    "cph": "Copenhagen",
    "kbh": "Copenhagen",
    "kobenhavn": "Copenhagen",
    "aar": "Aarhus",
    "aal": "Aalborg",
}

# Danish mainland bounding box (excludes Greenland, Faroe Islands)
DK_LAT_MIN, DK_LAT_MAX = 54.5, 57.8
DK_LON_MIN, DK_LON_MAX = 7.9, 15.2


# ---------------------------------------------------------------------------
# Alfred output helpers
# ---------------------------------------------------------------------------

def alfred_output(items: list) -> None:
    print(json.dumps({"items": items}, ensure_ascii=False))


def item(
    uid: str,
    title: str,
    subtitle: str = "",
    arg: str = "",
    valid: bool = False,
    quicklookurl: str = "",
) -> dict:
    d = {"uid": uid, "title": title, "subtitle": subtitle, "valid": valid}
    if arg:
        d["arg"] = arg
    if quicklookurl:
        d["quicklookurl"] = quicklookurl
    elif arg and valid:
        d["quicklookurl"] = arg
    return d


def error_item(msg: str, detail: str = "") -> list:
    return [item("error", f"⚠️ Error: {msg}", detail)]


def prompt_item() -> list:
    return [item(
        "prompt",
        "🔍 Type a city name",
        "e.g. Aarhus, Copenhagen, cph, Oslo",
    )]


# ---------------------------------------------------------------------------
# Geocoding via Open-Meteo (free, no API key)
# ---------------------------------------------------------------------------

def normalize(s: str) -> str:
    """Lowercase, strip diacritics, strip whitespace."""
    nfkd = unicodedata.normalize("NFKD", s)
    ascii_s = "".join(c for c in nfkd if not unicodedata.combining(c))
    return ascii_s.lower().strip()


def resolve_alias(query: str) -> str:
    """Expand known aliases."""
    nq = normalize(query)
    return ALIASES.get(nq, query)


def geocode(query: str) -> dict | None:
    """Geocode a city name via Open-Meteo."""
    params = urllib.parse.urlencode({"name": query, "count": 1})
    url = f"{GEOCODE_URL}?{params}"
    try:
        data = fetch_json(url)
        results = data.get("results")
        if not results:
            return None
        r = results[0]
        return {
            "id": r.get("id"),
            "name": r.get("name", query),
            "lat": r["latitude"],
            "lon": r["longitude"],
            "country_code": r.get("country_code", ""),
            "country": r.get("country", ""),
            "timezone": r.get("timezone"),
        }
    except Exception:
        return None


def dmi_location_url(geo: dict) -> str:
    """Build the public dmi.dk location page URL for a geocoded place."""
    location_id = geo.get("id")
    country_code = geo.get("country_code")
    if not location_id or not country_code:
        return "https://www.dmi.dk/"
    slug = "_".join(str(geo.get("name", "location")).split()).replace("/", "_")
    slug = urllib.parse.quote(slug, safe="_-")
    return DMI_LOCATION_PAGE_URL.format(
        country_code=country_code.upper(),
        location_id=location_id,
        slug=slug,
    )


def fetch_location_forecast(location_id: int | str | None, tz_name: str | None = None) -> dict | None:
    """Fetch the current DMI location forecast row for a geocoded place."""
    if not location_id:
        return None
    params = {"cmd": "llj", "id": str(location_id)}
    if tz_name:
        params["tz"] = tz_name
    url = f"{DMI_LOCATION_API_URL}?{urllib.parse.urlencode(params)}"
    try:
        data = fetch_json(url)
    except Exception:
        return None
    timeserie = data.get("timeserie") or []
    if not timeserie:
        return None
    row = timeserie[0]
    return {
        "temp_dry": row.get("temp"),
        "wind_speed": row.get("windSpeed"),
        "wind_dir": row.get("windDegree"),
        "wind_compass": row.get("windDir"),
        "wind_gust": row.get("windGust"),
        "wind_gust_label": "gusts",
        "precip_past1h": row.get("precip1"),
        "timestamp": row.get("localTimeIso") or row.get("time"),
        "city": data.get("city"),
        "country_code": data.get("country"),
    }


# ---------------------------------------------------------------------------
# DMI station cache + coordinate-based lookup (Synop only)
# ---------------------------------------------------------------------------

def cache_is_fresh() -> bool:
    try:
        age = datetime.now(timezone.utc).timestamp() - os.path.getmtime(STATIONS_CACHE)
        return age < CACHE_MAX_AGE_SECONDS
    except OSError:
        return False


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "dmi-alfred/3.0 (cyclist weather)"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_stations() -> list:
    """Returns list of active Synop station dicts (Danish mainland only), from cache or API."""
    if cache_is_fresh():
        with open(STATIONS_CACHE, encoding="utf-8") as f:
            return json.load(f)

    data = fetch_json(STATION_URL)
    stations = []
    seen = set()
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        coords = feat.get("geometry", {}).get("coordinates", [None, None])
        lat = coords[1]
        lon = coords[0]

        # Only Synop stations (full weather instruments)
        if props.get("type") != "Synop":
            continue
        # Only active stations with no end date
        if props.get("status") != "Active":
            continue
        if props.get("validTo") is not None:
            continue
        # Only Danish mainland
        if lat is None or lon is None:
            continue
        if not (DK_LAT_MIN <= lat <= DK_LAT_MAX and DK_LON_MIN <= lon <= DK_LON_MAX):
            continue

        sid = props.get("stationId")
        if sid in seen:
            continue
        seen.add(sid)

        stations.append({
            "stationId": sid,
            "name": props.get("name", ""),
            "lon": lon,
            "lat": lat,
        })

    with open(STATIONS_CACHE, "w", encoding="utf-8") as f:
        json.dump(stations, f)

    return stations


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def find_nearest_stations(lat: float, lon: float, stations: list, n: int = 3) -> list:
    """Return the N nearest stations sorted by distance, with distance_km attached."""
    for s in stations:
        s["distance_km"] = haversine_km(lat, lon, s["lat"], s["lon"])
    return sorted(stations, key=lambda s: s["distance_km"])[:n]


def find_best_station_observation(lat: float, lon: float, stations: list, n: int = 5) -> tuple:
    """Return the nearest station with usable observations, plus its data."""
    nearest = find_nearest_stations(lat, lon, stations, n=n)
    for candidate in nearest:
        candidate_obs = fetch_all_observations(candidate["stationId"])
        if has_useful_data(candidate_obs):
            return candidate, candidate_obs
    return None, None


# ---------------------------------------------------------------------------
# DMI observations
# ---------------------------------------------------------------------------

def fetch_observation(station_id: str, parameter_id: str) -> dict | None:
    """Fetch the most recent observation value for a station+parameter."""
    params = urllib.parse.urlencode({
        "stationId": station_id,
        "parameterId": parameter_id,
        "limit": "1",
        "sortorder": "observed,DESC",
    })
    url = f"{OBS_URL}?{params}"
    try:
        data = fetch_json(url)
        features = data.get("features", [])
        if features:
            props = features[0]["properties"]
            return {
                "value": props.get("value"),
                "observed": props.get("observed"),
            }
    except Exception:
        pass
    return None


def fetch_all_observations(station_id: str) -> dict:
    """Fetch temp, wind speed, wind dir, peak wind (wind_max), precipitation, and timestamp."""
    params_to_fetch = {
        "temp_dry": "temp_dry",
        "wind_speed": "wind_speed",
        "wind_dir": "wind_dir",
        "wind_gust": "wind_max",   # DMI obs API uses wind_max for peak wind
        "precip_past1h": "precip_past1h",
    }
    results = {"wind_gust_label": "max"}
    timestamps = []
    for key, parameter_id in params_to_fetch.items():
        obs = fetch_observation(station_id, parameter_id)
        results[key] = obs["value"] if obs else None
        if obs and obs.get("observed"):
            timestamps.append(obs["observed"])
    results["timestamp"] = timestamps[0] if timestamps else None
    return results


def has_useful_data(obs: dict) -> bool:
    """Check if observations have at least temperature or wind data."""
    return obs.get("temp_dry") is not None or obs.get("wind_speed") is not None


# ---------------------------------------------------------------------------
# Cyclist calculations
# ---------------------------------------------------------------------------

def degrees_to_compass(degrees: float) -> str:
    idx = round(degrees / 45) % 8
    return COMPASS_DIRS[idx]


def wind_chill(temp_c: float, wind_ms: float) -> float | None:
    """Returns wind chill in °C, or None if not meaningful."""
    if temp_c >= 10 or wind_ms < 1.3:
        return None
    v_kmh = wind_ms * 3.6
    wc = (13.12 + 0.6215 * temp_c
          - 11.37 * (v_kmh ** 0.16)
          + 0.3965 * temp_c * (v_kmh ** 0.16))
    return round(wc, 1)


def beaufort(wind_ms: float) -> tuple:
    """Returns (beaufort_number, label)."""
    thresholds = [
        (0.3,  0, "Calm"),
        (1.5,  1, "Light air"),
        (3.3,  2, "Light breeze"),
        (5.4,  3, "Gentle breeze"),
        (7.9,  4, "Moderate breeze"),
        (10.7, 5, "Fresh breeze"),
        (13.8, 6, "Strong breeze"),
        (17.1, 7, "Near gale"),
        (20.7, 8, "Gale"),
        (24.4, 9, "Strong gale"),
        (28.4, 10, "Storm"),
        (32.6, 11, "Violent storm"),
    ]
    for threshold, bf, label in thresholds:
        if wind_ms <= threshold:
            return bf, label
    return 12, "Hurricane force"


def cycling_rating(temp: float | None, wind: float | None, precip: float | None) -> tuple:
    """Returns (rating, narrative)."""
    reasons = []
    score = 0  # 0=good, 1=moderate, 2=poor

    if wind is not None:
        if wind > 10:
            score = max(score, 2)
            reasons.append(f"Strong wind {wind:.1f} m/s")
        elif wind > 7:
            score = max(score, 1)
            reasons.append(f"Noticeable wind {wind:.1f} m/s")

    if temp is not None:
        if temp < 0:
            score = max(score, 2)
            reasons.append("Below freezing — ice risk")
        elif temp < 3:
            score = max(score, 1)
            reasons.append(f"Cold {temp:.1f}°C — dress warmly")

    if precip is not None:
        if precip > 1:
            score = max(score, 2)
            reasons.append(f"Rain {precip:.1f} mm/h")
        elif precip > 0.1:
            score = max(score, 1)
            reasons.append(f"Light rain {precip:.1f} mm/h")

    labels = ["GOOD CONDITIONS", "MODERATE CONDITIONS", "POOR CONDITIONS"]
    defaults = [
        "No significant issues for cycling.",
        "Manageable, but take care.",
        "Consider alternative transport.",
    ]
    rating = labels[score]
    narrative = " ".join(reasons) if reasons else defaults[score]
    return rating, narrative


def rating_emoji(rating: str) -> str:
    prefix = rating.split()[0]
    return {"GOOD": "🚴", "MODERATE": "⚠️", "POOR": "⛔"}.get(prefix, "🚲")


def format_time_label(timestamp: str | None) -> str:
    if not timestamp:
        return datetime.now().strftime("%H:%M")
    try:
        if "T" in timestamp:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        else:
            dt = datetime.strptime(timestamp, "%Y%m%d%H%M%S")
        return dt.strftime("%H:%M")
    except ValueError:
        return datetime.now().strftime("%H:%M")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    query = sys.argv[1].strip() if len(sys.argv) > 1 else ""

    if not query:
        alfred_output(prompt_item())
        return

    # Resolve aliases (cph → Copenhagen, kbh → Copenhagen, etc.)
    query = resolve_alias(query)

    # Geocode the query → lat/lon + Open-Meteo location id
    geo = geocode(query)
    if not geo:
        alfred_output(error_item(
            f"City not found: \"{query}\"",
            "Try a city name like Aarhus, København, or cph"
        ))
        return

    city_name = geo["name"]
    city_lat = geo["lat"]
    city_lon = geo["lon"]
    country_code = geo["country_code"]
    dmi_url = dmi_location_url(geo)

    station = None
    obs = None
    source_kind = "forecast"
    source_note = f"DMI forecast · {city_name}"

    # For Danish cities, try live observations first
    if country_code == "DK":
        try:
            stations = load_stations()
        except Exception as e:
            alfred_output(error_item("Could not load DMI stations", str(e)))
            return

        if not stations:
            alfred_output(error_item("No active DMI Synop stations found"))
            return

        station, obs = find_best_station_observation(city_lat, city_lon, stations, n=5)
        if station and obs and station["distance_km"] <= OBSERVATION_MAX_DISTANCE_KM:
            source_kind = "observation"
            distance_km = station["distance_km"]
            dist_note = "nearby" if distance_km <= 5 else f"{distance_km:.0f} km away"
            source_note = f"Live obs · {station['name']} ({dist_note})"

    # Fall back to DMI location forecast (handles non-DK and distant DK stations)
    if source_kind != "observation":
        forecast = fetch_location_forecast(geo.get("id"), geo.get("timezone"))
        if forecast:
            obs = forecast
            forecast_city = forecast.get("city") or city_name
            if station and station.get("distance_km") is not None:
                source_note = (
                    f"DMI forecast · nearest obs station {station['name']} "
                    f"({station['distance_km']:.0f} km away)"
                )
            else:
                source_note = f"DMI forecast · {forecast_city}"
        elif station is not None and obs is not None:
            # Use the distant observation as a last resort
            source_kind = "observation"
            source_note = (
                f"Fallback obs · {station['name']} "
                f"({station['distance_km']:.0f} km away)"
            )
        else:
            alfred_output(error_item(
                f"No weather data for {city_name}",
                "Neither DMI live observations nor the DMI location forecast returned data"
            ))
            return

    temp = obs.get("temp_dry")
    wind_spd = obs.get("wind_speed")
    wind_deg = obs.get("wind_dir")
    wind_compass = obs.get("wind_compass")
    gust = obs.get("wind_gust")
    gust_label = obs.get("wind_gust_label") or "max"
    precip = obs.get("precip_past1h")

    # Format values
    temp_str = f"{temp:.1f}°C" if temp is not None else "N/A"
    wind_str = f"{wind_spd:.1f} m/s" if wind_spd is not None else "N/A"
    gust_str = f"{gust:.1f}" if gust is not None else "N/A"
    gust_title = gust_label.capitalize()
    compass = wind_compass or (degrees_to_compass(wind_deg) if wind_deg is not None else "?")
    compass_full = COMPASS_NAMES.get(compass, compass)
    wind_deg_str = f"{wind_deg:.0f}°" if wind_deg is not None else "?"
    precip_str = f"{precip:.1f} mm/h" if precip is not None else "N/A"
    time_label = format_time_label(obs.get("timestamp"))

    wc = wind_chill(temp, wind_spd) if (temp is not None and wind_spd is not None) else None
    feels_like = f"{wc:.1f}°C" if wc is not None else temp_str

    bf_num, bf_label = beaufort(wind_spd) if wind_spd is not None else (None, "N/A")
    bf_str = f"Beaufort {bf_num} — {bf_label}" if bf_num is not None else "N/A"

    rating, narrative = cycling_rating(temp, wind_spd, precip)
    mark = rating_emoji(rating)

    summary_title = f"{temp_str} — {compass} {wind_str} ({gust_label} {gust_str})"
    summary_subtitle = f"{mark} {rating} · {source_note} · {time_label}"

    alfred_output([
        item(
            "open",
            f"🌐 Open on dmi.dk: {city_name}",
            "Forecast page, hourly detail, radar and maps",
            arg=dmi_url,
            valid=True,
        ),
        item(
            "summary",
            f"📍 {summary_title}",
            summary_subtitle,
            arg=dmi_url,
            valid=True,
        ),
        item(
            "wind",
            f"🌬️ Wind: {compass_full} ({wind_deg_str}) · {wind_str} · {gust_title} {gust_str} m/s",
            bf_str,
            arg=dmi_url,
            valid=True,
        ),
        item(
            "temp",
            f"🌡️ Temperature: {temp_str} · Feels like {feels_like}",
            "Wind chill applies" if wc is not None else "No significant wind chill",
            arg=dmi_url,
            valid=True,
        ),
        item(
            "precip",
            f"🌧️ Precipitation: {precip_str}",
            "Dry conditions" if (precip is None or precip < 0.1) else "Bring rain gear",
            arg=dmi_url,
            valid=True,
        ),
        item(
            "rating",
            f"{mark} Cycling: {rating}",
            narrative,
            arg=dmi_url,
            valid=True,
        ),
    ])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        alfred_output(error_item("Unexpected error", str(e)))
