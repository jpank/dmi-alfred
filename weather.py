#!/usr/bin/env python3
"""
DMI Cyclist Weather — Alfred Script Filter
Fetches live DMI observations and outputs Alfred JSON for cyclist-relevant conditions.

Alfred setup:
  Script Filter → Language: /usr/bin/python3
  Script: python3 weather.py "{query}"
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

STATIONS_CACHE = "/tmp/dmi_stations.json"
CACHE_MAX_AGE_SECONDS = 86400  # 1 day

BASE_URL = "https://opendataapi.dmi.dk/v2/metObs/collections"
STATION_URL = f"{BASE_URL}/station/items?limit=500&country=DNK"
OBS_URL = f"{BASE_URL}/observation/items"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CITY_CSV = os.path.join(SCRIPT_DIR, "dmi_city_list.csv")

COMPASS_DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
COMPASS_NAMES = {
    "N": "North", "NE": "Northeast", "E": "East", "SE": "Southeast",
    "S": "South", "SW": "Southwest", "W": "West", "NW": "Northwest",
}


# ---------------------------------------------------------------------------
# Alfred output helpers
# ---------------------------------------------------------------------------

def alfred_output(items: list) -> None:
    print(json.dumps({"items": items}, ensure_ascii=False))


def item(uid: str, title: str, subtitle: str = "", arg: str = "", valid: bool = False) -> dict:
    d = {"uid": uid, "title": title, "subtitle": subtitle, "valid": valid}
    if arg:
        d["arg"] = arg
    return d


def error_item(msg: str, detail: str = "") -> list:
    return [item("error", f"Error: {msg}", detail)]


def prompt_item() -> list:
    return [item("prompt", "Type a Danish city or postcode", "e.g. Billund or 7190")]


# ---------------------------------------------------------------------------
# City CSV lookup
# ---------------------------------------------------------------------------

def normalize(s: str) -> str:
    """Lowercase, strip diacritics, strip whitespace."""
    nfkd = unicodedata.normalize("NFKD", s)
    ascii_s = "".join(c for c in nfkd if not unicodedata.combining(c))
    return ascii_s.lower().strip()


def load_cities() -> dict:
    """Returns {normalized_city_name: (postcode, original_name)} and {postcode_str: (postcode, original_name)}."""
    by_name: dict = {}
    by_postcode: dict = {}
    try:
        with open(CITY_CSV, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",", 1)
                if len(parts) != 2:
                    continue
                postcode_str = parts[0].strip()
                city_name = parts[1].strip()
                entry = (postcode_str, city_name)
                by_name[normalize(city_name)] = entry
                by_postcode[postcode_str] = entry
    except FileNotFoundError:
        pass
    return by_name, by_postcode


def find_city(query: str, by_name: dict, by_postcode: dict):
    """Returns (postcode_str, city_name) or None."""
    q = query.strip()
    if q.isdigit():
        return by_postcode.get(q)

    nq = normalize(q)
    # Exact match
    if nq in by_name:
        return by_name[nq]
    # Prefix / substring match
    candidates = [(k, v) for k, v in by_name.items() if nq in k]
    if candidates:
        # Prefer shortest name (closest match)
        candidates.sort(key=lambda x: len(x[0]))
        return candidates[0][1]
    return None


# ---------------------------------------------------------------------------
# DMI station cache
# ---------------------------------------------------------------------------

def cache_is_fresh() -> bool:
    try:
        age = datetime.now(timezone.utc).timestamp() - os.path.getmtime(STATIONS_CACHE)
        return age < CACHE_MAX_AGE_SECONDS
    except OSError:
        return False


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "dmi-alfred/2.0 (cyclist weather)"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_stations() -> list:
    """Returns list of station dicts from cache or API."""
    if cache_is_fresh():
        with open(STATIONS_CACHE, encoding="utf-8") as f:
            return json.load(f)

    data = fetch_json(STATION_URL)
    stations = []
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        coords = feat.get("geometry", {}).get("coordinates", [None, None])
        stations.append({
            "stationId": props.get("stationId"),
            "name": props.get("name", ""),
            "municipality": props.get("municipalityName", ""),
            "lon": coords[0],
            "lat": coords[1],
        })

    with open(STATIONS_CACHE, "w", encoding="utf-8") as f:
        json.dump(stations, f)

    return stations


def find_station(city_name: str, stations: list) -> dict | None:
    """Find best matching station for city_name."""
    nc = normalize(city_name)

    # 1. Exact municipality match
    for s in stations:
        if normalize(s["municipality"]) == nc:
            return s

    # 2. Station name exact match
    for s in stations:
        if normalize(s["name"]) == nc:
            return s

    # 3. Municipality contains query
    candidates = [s for s in stations if nc in normalize(s["municipality"])]
    if candidates:
        return candidates[0]

    # 4. Station name contains query
    candidates = [s for s in stations if nc in normalize(s["name"])]
    if candidates:
        return candidates[0]

    # 5. Query contains municipality name (for short city names matching large municipalities)
    candidates = [s for s in stations if normalize(s["municipality"]) in nc]
    if candidates:
        return candidates[0]

    return None


# ---------------------------------------------------------------------------
# DMI observations
# ---------------------------------------------------------------------------

def fetch_observation(station_id: str, parameter_id: str) -> float | None:
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
            return features[0]["properties"]["value"]
    except Exception:
        pass
    return None


def fetch_all_observations(station_id: str) -> dict:
    """Fetch temp, wind speed, wind dir, gusts, precipitation."""
    params_to_fetch = ["temp_dry", "wind_speed", "wind_dir", "wind_gust", "precip_past1h"]
    results = {}
    for param in params_to_fetch:
        results[param] = fetch_observation(station_id, param)
    return results


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


def beaufort(wind_ms: float) -> tuple[int, str]:
    """Returns (beaufort_number, label)."""
    thresholds = [
        (0.3, 0, "Calm"),
        (1.5, 1, "Light air"),
        (3.3, 2, "Light breeze"),
        (5.4, 3, "Gentle breeze"),
        (7.9, 4, "Moderate breeze"),
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


def cycling_rating(temp: float | None, wind: float | None, precip: float | None) -> tuple[str, str]:
    """Returns (rating, narrative)."""
    reasons = []
    score = 0  # 0=good, 1=moderate, 2=poor

    if wind is not None:
        if wind > 10:
            score = max(score, 2)
            reasons.append(f"Strong wind {wind:.1f} m/s")
        elif wind > 7:
            score = max(score, 1)
            reasons.append(f"Noticeable headwind {wind:.1f} m/s")
        else:
            pass  # fine

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
    defaults = ["No significant issues for cycling.", "Manageable, but take care.", "Consider alternative transport."]

    rating = labels[score]
    narrative = " ".join(reasons) if reasons else defaults[score]
    return rating, narrative


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    query = sys.argv[1].strip() if len(sys.argv) > 1 else ""

    if not query:
        alfred_output(prompt_item())
        return

    # City lookup
    by_name, by_postcode = load_cities()
    city_match = find_city(query, by_name, by_postcode)
    if not city_match:
        alfred_output(error_item(
            f"No city found for \"{query}\"",
            "Try a Danish city name or 4-digit postcode"
        ))
        return

    postcode_str, city_name = city_match

    # Station lookup
    try:
        stations = load_stations()
    except Exception as e:
        alfred_output(error_item("Could not load DMI stations", str(e)))
        return

    station = find_station(city_name, stations)
    if not station:
        alfred_output(error_item(
            f"No DMI station found near {city_name}",
            "Try a nearby city"
        ))
        return

    # Fetch observations
    try:
        obs = fetch_all_observations(station["stationId"])
    except Exception as e:
        alfred_output(error_item("DMI API error", str(e)))
        return

    temp = obs.get("temp_dry")
    wind_spd = obs.get("wind_speed")
    wind_deg = obs.get("wind_dir")
    gust = obs.get("wind_gust")
    precip = obs.get("precip_past1h")

    # Format values
    temp_str = f"{temp:.1f}°C" if temp is not None else "N/A"
    wind_str = f"{wind_spd:.1f} m/s" if wind_spd is not None else "N/A"
    gust_str = f"{gust:.1f}" if gust is not None else "?"
    compass = degrees_to_compass(wind_deg) if wind_deg is not None else "?"
    compass_full = COMPASS_NAMES.get(compass, compass)
    wind_deg_str = f"{wind_deg:.0f}°" if wind_deg is not None else "?"
    precip_str = f"{precip:.1f} mm/h" if precip is not None else "N/A"
    now = datetime.now().strftime("%H:%M")

    wc = wind_chill(temp, wind_spd) if (temp is not None and wind_spd is not None) else None
    feels_like = f"{wc:.1f}°C" if wc is not None else temp_str

    bf_num, bf_label = beaufort(wind_spd) if wind_spd is not None else (None, "N/A")
    bf_str = f"Beaufort {bf_num} — {bf_label}" if bf_num is not None else "N/A"

    rating, narrative = cycling_rating(temp, wind_spd, precip)

    # Summary line
    summary_title = f"{temp_str} — {compass} {wind_str} (gusts {gust_str})"
    rating_emoji = {"GOOD": "✓", "MODERATE": "⚠", "POOR": "✗"}.get(rating.split()[0], "")
    summary_subtitle = f"{rating_emoji} {rating} · {station['name']} · Updated {now}"

    dmi_url = f"https://www.dmi.dk/lokation/show/DK/{postcode_str}/{urllib.parse.quote(city_name)}/"

    items = [
        item("summary", summary_title, summary_subtitle, arg=dmi_url, valid=True),
        item(
            "wind",
            f"Wind: {compass_full} ({wind_deg_str}) · {wind_str} · Gusts {gust_str} m/s",
            bf_str,
        ),
        item(
            "temp",
            f"Temperature: {temp_str} · Feels like {feels_like}",
            f"Wind chill applies" if wc is not None else "No significant wind chill",
        ),
        item(
            "precip",
            f"Precipitation: {precip_str}",
            "Dry conditions" if (precip is None or precip < 0.1) else "Bring rain gear",
        ),
        item(
            "rating",
            f"Cycling: {rating}",
            narrative,
        ),
    ]

    alfred_output(items)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        alfred_output(error_item("Unexpected error", str(e)))
