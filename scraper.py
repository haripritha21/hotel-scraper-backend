import asyncio
import logging
import urllib.request
import json
from functools import lru_cache
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)


@lru_cache(maxsize=256)
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


def _overpass_query(lat, lon, radius_m, max_results):
    query = f"""
    [out:json][timeout:15];
    (
      node["amenity"~"restaurant|cafe|fast_food|food_court|bar|juice_bar|sweet_shop|canteen|dhaba|tiffin|mess|bakery|ice_cream"](around:{radius_m},{lat},{lon});
      node["amenity"="hotel"](around:{radius_m},{lat},{lon});
      node["building"~"restaurant|hotel"](around:{radius_m},{lat},{lon});
    );
    out body {max_results};
    """
    mirrors = [
        "https://overpass.kumi.systems/api/interpreter",
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
        "https://overpass-api.de/api/interpreter",
    ]
    data = query.encode("utf-8")
    for mirror in mirrors:
        try:
            req = urllib.request.Request(mirror, data=data, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            req.add_header("User-Agent", "HotelScraper/1.0 (educational project)")
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                elements = result.get("elements", [])
                logger.info("Overpass returned %d elements", len(elements))
                return elements
        except Exception as e:
            logger.warning("Mirror %s failed: %s", mirror, e)
            continue
    return []


BLOCKED_CATEGORIES = (
    "guest_house", "lodge", "resort", "hostel",
    "motel", "dormitory", "apartment",
)

BLOCKED_NAMES = (
    "lodge", "guest house", "guesthouse", "resort",
    "hostel", "dormitory", "homestay", "paying guest",
)


def _parse_element(element, idx):
    tags = element.get("tags", {})
    name = tags.get("name") or tags.get("name:en") or tags.get("name:ta")
    if not name or len(name.strip()) < 2:
        return None

    addr_parts = []
    for key in ["addr:housenumber", "addr:street", "addr:suburb", "addr:city", "addr:state"]:
        val = tags.get(key)
        if val:
            addr_parts.append(val)
    address = ", ".join(addr_parts) if addr_parts else tags.get("addr:full", "N/A")

    phone  = tags.get("phone") or tags.get("contact:phone") or "N/A"
    rating = tags.get("stars") or tags.get("rating") or "N/A"

    amenity  = tags.get("amenity", "")
    tourism  = tags.get("tourism", "")
    building = tags.get("building", "")
    category = amenity or tourism or building

    if any(x in category.lower() for x in BLOCKED_CATEGORIES):
        return None
    if any(x in name.lower() for x in BLOCKED_NAMES):
        return None

    return {
        "sno":      idx + 1,
        "name":     name.strip(),
        "address":  address,
        "phone":    phone,
        "rating":   str(rating),
        "source":   "OpenStreetMap",
        "category": category,
    }


async def fetch_places(location: str, radius_km: float, max_results: int = 10) -> list:
    max_results = max(1, min(int(max_results or 10), 50))
    radius_m    = int(radius_km * 1000)

    lat, lon = await asyncio.to_thread(_geocode_location, location)
    if lat is None:
        logger.warning("Could not geocode: %s", location)
        return []

    elements = await asyncio.to_thread(_overpass_query, lat, lon, radius_m, max_results * 2)
    if not elements:
        return []

    results    = []
    seen_names = set()

    for element in elements:
        if len(results) >= max_results:
            break
        parsed = _parse_element(element, len(results))
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
