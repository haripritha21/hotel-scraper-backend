import asyncio
import logging
import urllib.request
import json
import os
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

FSQ_API_KEY = os.environ.get("FSQ_API_KEY", "")

BLOCKED_NAMES = (
    "lodge", "guest house", "guesthouse", "resort", "hostel",
    "motel", "dormitory", "homestay", "paying guest",
    "tasmac", "liquor", "wine shop", "bar", "pub", "beer", "toddy",
)

FOOD_CATEGORY_IDS = "13000,13065,13032,13046,13064,13031,13062,13040,13034"


def _geocode_location(location: str):
    parts = [p.strip() for p in location.replace("+", " ").split(",")]
    headers = {"User-Agent": "HotelScraper/1.0 (educational project)"}
    for i in range(len(parts)):
        query = ",".join(parts[i:])
        try:
            url = f"https://nominatim.openstreetmap.org/search?q={quote_plus(query)}&format=json&limit=1"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                if data:
                    lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
                    logger.info("Geocoded %r -> %.5f, %.5f", query, lat, lon)
                    return lat, lon
        except Exception as e:
            logger.debug("Geocode failed for %r: %s", query, e)
            continue
    logger.warning("Geocoding failed for %r", location)
    return None, None


def _foursquare_search(lat: float, lon: float, radius_m: int, max_results: int) -> list:
    url = (
        f"https://api.foursquare.com/v3/places/search"
        f"?ll={lat},{lon}"
        f"&radius={radius_m}"
        f"&categories={FOOD_CATEGORY_IDS}"
        f"&limit={min(max_results, 50)}"
        f"&fields=name,location,tel,rating,categories"
    )
    headers = {
        "Authorization": FSQ_API_KEY,
        "Accept": "application/json",
        "User-Agent": "HotelScraper/1.0 (educational project)",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            results = data.get("results", [])
            logger.info("Foursquare returned %d results", len(results))
            return results
    except Exception as e:
        logger.error("Foursquare API failed: %s", e)
        return []


def _parse_place(place: dict, idx: int):
    name = place.get("name", "").strip()
    if not name or len(name) < 2:
        return None

    name_lower = name.lower()
    if any(x in name_lower for x in BLOCKED_NAMES):
        return None

    categories = place.get("categories", [])
    cat_name = categories[0].get("name", "") if categories else ""
    if any(x in cat_name.lower() for x in ("bar", "pub", "liquor", "wine", "nightclub")):
        return None

    location = place.get("location", {})
    addr_parts = []
    for key in ["address", "locality", "region"]:
        val = location.get(key)
        if val:
            addr_parts.append(val)
    address = ", ".join(addr_parts) if addr_parts else "N/A"

    phone  = place.get("tel", "N/A") or "N/A"
    rating = place.get("rating", "N/A")
    if rating != "N/A":
        rating = str(round(float(rating), 1))

    return {
        "sno":      idx + 1,
        "name":     name,
        "address":  address,
        "phone":    phone,
        "rating":   str(rating),
        "source":   "Foursquare",
        "category": cat_name,
    }


async def fetch_places(location: str, radius_km: float, max_results: int = 10) -> list:
    max_results = max(1, min(int(max_results or 10), 50))
    radius_m    = int(radius_km * 1000)

    lat, lon = await asyncio.to_thread(_geocode_location, location)
    if lat is None:
        logger.warning("Could not geocode: %s", location)
        return []

    logger.info("Foursquare: %.5f,%.5f radius=%dm max=%d", lat, lon, radius_m, max_results)
    places = await asyncio.to_thread(_foursquare_search, lat, lon, radius_m, max_results)

    if not places:
        return []

    results    = []
    seen_names = set()

    for place in places:
        if len(results) >= max_results:
            break
        parsed = _parse_place(place, len(results))
        if not parsed:
            continue
        name_key = parsed["name"].lower().strip()
        if name_key in seen_names:
            continue
        seen_names.add(name_key)
        parsed["sno"] = len(results) + 1
        results.append(parsed)

    def _rating_val(r):
        try:
            return float(r.get("rating", "0"))
        except Exception:
            return 0.0

    results.sort(key=_rating_val, reverse=True)
    logger.info("Returning %d results for %r", len(results), location)
    return results
