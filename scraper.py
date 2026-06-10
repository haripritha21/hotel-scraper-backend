"""
Hotel scraper using Overpass API (OpenStreetMap) — no Playwright/Chrome needed.
Finds hotels, lodges, resorts, guest houses near a location using OSM data.
"""

import asyncio
import logging
import urllib.request
import urllib.parse
import json
from math import radians, sin, cos, sqrt, atan2
from typing import Optional

logger = logging.getLogger(__name__)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def _geocode_location(location: str) -> tuple[Optional[float], Optional[float]]:
    """Convert location name to lat/lon using Nominatim (OpenStreetMap)."""
    parts = [p.strip() for p in location.replace("+", " ").split(",")]
    queries_to_try = [",".join(parts[i:]) for i in range(len(parts))]
    headers = {"User-Agent": "HotelFinder/1.0 (educational project)"}

    for query in queries_to_try:
        try:
            url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote_plus(query)}&format=json&limit=1"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                if data:
                    lat = float(data[0]["lat"])
                    lon = float(data[0]["lon"])
                    logger.info("Geocoded %r → %.5f, %.5f", query, lat, lon)
                    return lat, lon
        except Exception as e:
            logger.debug("Geocode failed for %r: %s", query, e)
            continue

    logger.warning("Geocoding failed for %r", location)
    return None, None


def _fetch_hotels_overpass(lat: float, lon: float, radius_km: float, max_results: int) -> list[dict]:
    """Fetch hotels from Overpass API within radius_km of lat/lon."""
    radius_m = int(radius_km * 1000)

    # Query for hotels, lodges, resorts, guest houses, hostels
    query = f"""
    [out:json][timeout:30];
    (
      node["tourism"="hotel"](around:{radius_m},{lat},{lon});
      node["tourism"="guest_house"](around:{radius_m},{lat},{lon});
      node["tourism"="hostel"](around:{radius_m},{lat},{lon});
      node["tourism"="motel"](around:{radius_m},{lat},{lon});
      node["tourism"="resort"](around:{radius_m},{lat},{lon});
      node["building"="hotel"](around:{radius_m},{lat},{lon});
      way["tourism"="hotel"](around:{radius_m},{lat},{lon});
      way["tourism"="guest_house"](around:{radius_m},{lat},{lon});
      way["building"="hotel"](around:{radius_m},{lat},{lon});
    );
    out body center {max_results * 3};
    """

    url = "https://overpass-api.de/api/interpreter"
    data = urllib.parse.urlencode({"data": query}).encode()
    headers = {
        "User-Agent": "HotelFinder/1.0 (educational project)",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=35) as resp:
        return json.loads(resp.read().decode()).get("elements", [])


def _parse_element(element: dict, center_lat: float, center_lon: float) -> Optional[dict]:
    """Parse an Overpass element into a hotel dict."""
    tags = element.get("tags", {})

    name = tags.get("name") or tags.get("name:en") or tags.get("brand")
    if not name:
        return None

    # Get coordinates
    if element["type"] == "node":
        place_lat = element.get("lat")
        place_lon = element.get("lon")
    else:
        # way — use center
        center = element.get("center", {})
        place_lat = center.get("lat")
        place_lon = center.get("lon")

    dist_km = None
    if place_lat and place_lon:
        dist_km = round(_haversine_km(center_lat, center_lon, place_lat, place_lon), 2)

    # Build address
    addr_parts = []
    for key in ["addr:housenumber", "addr:street", "addr:suburb", "addr:city", "addr:state"]:
        val = tags.get(key)
        if val:
            addr_parts.append(val)
    address = ", ".join(addr_parts) if addr_parts else tags.get("addr:full", "N/A")

    # Phone
    phone = tags.get("phone") or tags.get("contact:phone") or tags.get("telephone") or "N/A"

    # Rating (OSM sometimes has stars)
    stars = tags.get("stars") or tags.get("rating")
    rating = stars if stars else "N/A"

    # Category
    tourism = tags.get("tourism", "")
    category_map = {
        "hotel": "Hotel",
        "guest_house": "Guest House",
        "hostel": "Hostel",
        "motel": "Motel",
        "resort": "Resort",
    }
    category = category_map.get(tourism, "Hotel")

    return {
        "name": name,
        "address": address,
        "phone": phone,
        "rating": rating,
        "category": category,
        "website": tags.get("website") or tags.get("contact:website") or "N/A",
        "_distance_km": dist_km,
    }


async def fetch_places(location: str, radius_km: float, max_results: int = 10) -> list[dict]:
    """Main entry point — fetch hotels near location using Overpass API."""
    max_results = max(1, min(int(max_results or 10), 100))

    # Step 1: Geocode location
    center_lat, center_lon = await asyncio.to_thread(_geocode_location, location)
    if center_lat is None:
        logger.warning("Could not geocode location: %s", location)
        return []

    logger.info("Searching hotels near %.5f, %.5f radius=%.1f km", center_lat, center_lon, radius_km)

    # Step 2: Fetch from Overpass
    try:
        elements = await asyncio.to_thread(
            _fetch_hotels_overpass, center_lat, center_lon, radius_km, max_results
        )
    except Exception as e:
        logger.error("Overpass API error: %s", e)
        return []

    logger.info("Overpass returned %d elements", len(elements))

    # Step 3: Parse results
    items: list[dict] = []
    name_seen: set[str] = set()

    for element in elements:
        if len(items) >= max_results:
            break

        entry = _parse_element(element, center_lat, center_lon)
        if not entry:
            continue

        name_key = entry["name"].lower().strip()
        if name_key in name_seen:
            continue

        name_seen.add(name_key)
        items.append(entry)
        logger.info(
            "  ✓ %-35s [%.1f km]",
            entry["name"],
            entry.get("_distance_km") or 0,
        )

    # Sort by distance
    items.sort(key=lambda x: x.get("_distance_km") or 999)

    return items[:max_results]
