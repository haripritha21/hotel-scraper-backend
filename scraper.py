"""
Google Maps hotel scraper — v4 (hotel-finder fixed)

Fixes from v3:
  - Category allowlist now includes hotel/lodge/resort/inn/hostel
  - Category blocklist now blocks restaurants/mess/cafe/bakery
  - Search query changed to "hotels near {location}"
"""

import asyncio
import logging
import re
import sys
import urllib.request
import json
from math import radians, sin, cos, sqrt, atan2
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse, quote_plus

from playwright.async_api import (
    Page,
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

logger = logging.getLogger(__name__)

# ── Timing constants ──────────────────────────────────────────────────────────
ELEMENT_WAIT  = 10_000
PAGE_TIMEOUT  = 60_000
SETTLE_WAIT   = 1.8
PARALLEL_TABS = 2

# ── Junk-name filters ─────────────────────────────────────────────────────────
BLOCKED_EXACT: set[str] = {
    "results", "more results", "see more results", "all results",
    "open in google maps", "back to results", "search nearby",
    "save", "directions", "nearby", "all filters", "open now",
    "top rated", "price", "type", "sponsored", "ad",
    "suggest an edit", "add a photo", "share", "send to phone",
    "overview", "reviews", "photos", "menu", "about",
}
BLOCKED_PARTIAL: tuple[str, ...] = (
    "open in google",
    "get directions",
    "more photos",
    "add missing",
    "suggest an",
)

# ── CSS selectors ─────────────────────────────────────────────────────────────
FEED_CSS = ['div[role="feed"]', '.m6QErb[aria-label]', 'div.m6QErb']

CARD_LINKS = [
    "a.hfpxzc",
    "a.hfpxzc[href*='/maps/place/']",
    "div[role='feed'] a[href*='maps/place']",
    "[data-result-index] a",
]

TITLE_CSS = [
    "h1.DUwDvf",
    'h1[class*="fontHeadlineLarge"]',
    "h1",
]

STARS_CSS = [
    "div.F7nice > span > span[aria-hidden='true']",
    "span.MW4etd",
    "div.UY7F9 span[aria-hidden='true']",
    "span.ceNzKf",
]

ADDR_CSS = [
    "button[data-item-id='address']",
    "[data-tooltip='Copy address'] button",
    "button.CsEnBe[data-item-id='address']",
    "div[data-id='address']",
]

TEL_CSS = [
    "button[data-item-id*='phone:tel']",
    "button[data-tooltip*='phone'] span.Io6YTe",
    "button[data-item-id*='pn:'] span.Io6YTe",
    "[aria-label*='Phone'] span",
]

KIND_CSS = [
    "button.DkEaL",
    "span.YhemCb",
    "button.skqShb",
    "div.LBgpqf button",
]

SITE_CSS = [
    "a[data-item-id='authority']",
    "a[data-tooltip='Open website']",
    "a[aria-label*='Website']",
    "a[href^='http']:has(span.Io6YTe)",
]


# ── Distance helpers ──────────────────────────────────────────────────────────

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def _extract_coords_from_url(url: str) -> tuple[Optional[float], Optional[float]]:
    m = re.search(r'/@(-?\d+\.\d+),(-?\d+\.\d+)', url)
    if m:
        return float(m.group(1)), float(m.group(2))
    m2 = re.search(r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)', url)
    if m2:
        return float(m2.group(1)), float(m2.group(2))
    return None, None


def _address_matches_location(address: str, location: str) -> bool:
    addr_lower = address.lower()
    for word in location.lower().split():
        if len(word) > 2 and word in addr_lower:
            return True
    return False


def _geocode_location(location: str) -> tuple[Optional[float], Optional[float]]:
    parts = [p.strip() for p in location.replace("+", " ").split(",")]
    queries_to_try = []
    for i in range(len(parts)):
        queries_to_try.append(",".join(parts[i:]))

    headers = {"User-Agent": "HotelScraper/1.0 (educational project)"}

    for query in queries_to_try:
        try:
            url = f"https://nominatim.openstreetmap.org/search?q={quote_plus(query)}&format=json&limit=1"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                if data:
                    lat = float(data[0]["lat"])
                    lon = float(data[0]["lon"])
                    logger.info("Geocoded %r → %.5f, %.5f", query, lat, lon)
                    return lat, lon
        except Exception as e:
            logger.debug("Geocode attempt failed for %r: %s", query, e)
            continue

    logger.warning("Geocoding failed for all variants of %r", location)
    return None, None


# ── Public entry point ────────────────────────────────────────────────────────

async def fetch_places(location: str, radius_km: float, max_results: int = 10) -> list[dict]:
    output: list[dict] = []
    max_results = max(1, min(int(max_results or 10), 100))

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1280,900",
            ],
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        async def block_heavy_resources(route):
            if route.request.resource_type in ("image", "media", "font"):
                await route.abort()
            else:
                await route.continue_()

        await ctx.route("**/*", block_heavy_resources)

        page = await ctx.new_page()
        try:
            output = await _collect_results(page, location, radius_km, max_results)
        except Exception as exc:
            logger.error("Session error: %s", exc, exc_info=True)
        finally:
            await browser.close()

    output.sort(key=_parse_rating, reverse=True)
    return output[:max_results]


async def fetch_places_threaded(location: str, radius_km: float, max_results: int = 10) -> list[dict]:
    return await asyncio.to_thread(_fetch_sync, location, radius_km, max_results)


def _fetch_sync(location: str, radius_km: float, max_results: int = 10) -> list[dict]:
    if sys.platform.startswith("win") and hasattr(asyncio, "ProactorEventLoop"):
        loop = asyncio.ProactorEventLoop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(fetch_places(location, radius_km, max_results))
        finally:
            asyncio.set_event_loop(None)
            loop.close()
    return asyncio.run(fetch_places(location, radius_km, max_results))


# ── Core logic ────────────────────────────────────────────────────────────────

async def _collect_results(
    page: Page, location: str, radius_km: float, max_results: int
) -> list[dict]:
    # ✅ FIX 2: Search query — "hotels near" instead of "hotels+restaurants+near"
    query_url = (
        f"https://www.google.com/maps/search/"
        f"hotels+near+{location.replace(' ', '+')}"
        f"?hl=en"
    )
    logger.info("Opening → %s", query_url)
    await page.goto(query_url, wait_until="networkidle", timeout=PAGE_TIMEOUT)
    await _handle_popups(page)

    center_lat, center_lon = await asyncio.to_thread(_geocode_location, location)
    if center_lat:
        logger.info("Search centre (geocoded): %.5f, %.5f  radius=%.1f km", center_lat, center_lon, radius_km)

    if center_lat is None:
        await asyncio.sleep(2)
        current_url = page.url
        center_lat, center_lon = _extract_coords_from_url(current_url)
        if center_lat:
            logger.info("Search centre (page URL): %.5f, %.5f", center_lat, center_lon)
    else:
        await asyncio.sleep(2)

    feed_ready = False
    for sel in FEED_CSS:
        try:
            await page.wait_for_selector(sel, timeout=ELEMENT_WAIT)
            feed_ready = True
            logger.info("Feed confirmed via: %s", sel)
            break
        except PlaywrightTimeoutError:
            continue

    if not feed_ready:
        logger.warning("Feed not loaded for: %s", location)
        return []

    passes = min(24, max(6, (max_results // 5) + 4))
    await _scroll_panel(page, passes=passes)

    href_list: list[str] = []
    for sel in CARD_LINKS:
        nodes = await page.query_selector_all(sel)
        if not nodes:
            continue
        for node in nodes:
            href = await node.get_attribute("href")
            if not href:
                continue
            if href.startswith("/"):
                href = f"https://www.google.com{href}"
            if "/maps/place/" not in href:
                continue
            if href not in href_list:
                href_list.append(href)
        if href_list:
            logger.info("Gathered %d URLs via: %s", len(href_list), sel)
            break

    if not href_list:
        logger.warning("No place URLs found")
        return []

    if center_lat is None:
        for href in href_list[:5]:
            lat, lon = _extract_coords_from_url(href)
            if lat is not None:
                center_lat, center_lon = lat, lon
                logger.info("Search centre (first href): %.5f, %.5f", center_lat, center_lon)
                break

    if center_lat is None:
        logger.warning("All geocoding tiers failed — radius filter will use address keywords only")

    items: list[dict]   = []
    name_seen: set[str] = set()
    addr_seen: set[str] = set()

    limit = min(len(href_list), max(60, max_results * 6))
    candidates = href_list[:limit]

    sem = asyncio.Semaphore(PARALLEL_TABS)

    async def scrape_one(href: str) -> Optional[dict]:
        async with sem:
            tab = await page.context.new_page()
            try:
                return await _parse_place_page(tab, href)
            except Exception as exc:
                logger.warning("  Parse error: %s", exc)
                return None
            finally:
                await tab.close()

    logger.info("Scraping %d candidates with %d parallel tabs", len(candidates), PARALLEL_TABS)
    raw_results = await asyncio.gather(*[scrape_one(h) for h in candidates])

    for href, entry in zip(candidates, raw_results):
        if len(items) >= max_results:
            break

        if not entry or not entry.get("name"):
            continue

        raw_name = entry["name"].strip().lower()
        if len(raw_name) < 3:
            continue
        if raw_name in BLOCKED_EXACT:
            continue
        if any(s in raw_name for s in BLOCKED_PARTIAL):
            continue

        name_key = re.sub(r"[^a-z0-9]", "", raw_name)
        if name_key in name_seen:
            continue

        addr     = entry.get("address", "N/A")
        category = entry.get("category", "").lower()

        # ✅ FIX 1: Hotel category filter — allow lodges/resorts, block restaurants
        allowed  = ("hotel", "lodge", "resort", "inn", "hostel", "guest house", "homestay", "suites", "accommodation")
        blocked  = ("restaurant", "mess", "cafe", "fast food", "bakery", "bar", "pub", "dhaba")

        if any(x in category for x in blocked):
            continue
        if category and not any(x in category for x in allowed):
            continue

        place_lat, place_lon = _extract_coords_from_url(href)

        if place_lat is not None and center_lat is not None:
            dist_km = _haversine_km(center_lat, center_lon, place_lat, place_lon)
            entry["_distance_km"] = round(dist_km, 2)
            if dist_km > radius_km:
                logger.info(
                    "  ✗ SKIP (%.1f km > %.1f km radius) — %s", dist_km, radius_km, entry["name"]
                )
                continue
        elif center_lat is not None and place_lat is None:
            if addr and addr != "N/A" and not _address_matches_location(addr, location):
                logger.info(
                    "  ✗ SKIP (no place coords + address mismatch) — %s | %s", entry["name"], addr
                )
                continue
        else:
            if addr and addr != "N/A" and not _address_matches_location(addr, location):
                logger.info(
                    "  ✗ SKIP (address mismatch) — %s | %s", entry["name"], addr
                )
                continue

        if addr and addr != "N/A":
            addr_key = re.sub(r"\s+", " ", addr.lower().strip())[:100]
            if addr_key in addr_seen:
                continue
            addr_seen.add(addr_key)

        name_seen.add(name_key)
        items.append(entry)
        dist_str = f"{entry.get('_distance_km', '?')} km" if "_distance_km" in entry else "addr-match"
        logger.info("  ✓ [%d] %-35s ★%s  [%s]", len(items), entry["name"], entry.get("rating", "?"), dist_str)

    return items


async def _parse_place_page(page: Page, url: str) -> Optional[dict]:
    await page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT)
    try:
        await page.wait_for_selector("h1", timeout=ELEMENT_WAIT)
        await asyncio.sleep(SETTLE_WAIT)
    except PlaywrightTimeoutError:
        return None

    rec: dict = {}
    rec["name"]     = await _get_text(page, TITLE_CSS)

    raw_stars = await _get_text(page, STARS_CSS)
    if raw_stars and raw_stars != "N/A":
        m = re.search(r"(\d+[.,]\d+|\d+)", raw_stars)
        rec["rating"] = m.group(1).replace(",", ".") if m else raw_stars
    else:
        rec["rating"] = "N/A"

    rec["address"]  = await _get_text(page, ADDR_CSS)
    rec["phone"]    = await _get_text(page, TEL_CSS)
    rec["category"] = await _get_text(page, KIND_CSS)
    rec["website"]  = await _get_link(page, SITE_CSS)

    return rec if rec.get("name") and rec["name"] != "N/A" else None


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_text(page: Page, selectors: list[str]) -> str:
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                val = await el.text_content()
                if val and val.strip():
                    return _sanitise(val)
        except Exception:
            continue
    return "N/A"


async def _get_link(page: Page, selectors: list[str]) -> str:
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if not el:
                continue
            for attr in ("href", "aria-label"):
                raw = await el.get_attribute(attr)
                url = _normalise_url(raw)
                if url != "N/A":
                    return url
            val = await el.text_content()
            url = _normalise_url(val)
            if url != "N/A":
                return url
        except Exception:
            continue

    try:
        candidates = await page.evaluate(
            """
            () => Array.from(document.querySelectorAll('a')).map((a) => ({
                href: a.href || '',
                aria: a.getAttribute('aria-label') || '',
                item: a.getAttribute('data-item-id') || '',
                tooltip: a.getAttribute('data-tooltip') || '',
                text: a.textContent || ''
            })).filter((a) => {
                const h = `${a.href} ${a.aria} ${a.item} ${a.tooltip} ${a.text}`.toLowerCase();
                return h.includes('website') || h.includes('authority');
            })
            """
        )
        for c in candidates:
            for key in ("href", "aria", "tooltip", "text", "item"):
                url = _normalise_url(c.get(key))
                if url != "N/A":
                    return url
    except Exception:
        pass

    return "N/A"


def _normalise_url(value: Optional[str]) -> str:
    if not value:
        return "N/A"
    raw = _sanitise(value).replace("Website:", "", 1).strip()
    if raw == "N/A":
        return "N/A"
    parsed = urlparse(raw)

    if parsed.netloc and "google." in parsed.netloc:
        params = parse_qs(parsed.query)
        for key in ("q", "url"):
            target = params.get(key, [""])[0]
            if target:
                return _normalise_url(unquote(target))
        return "N/A"

    if parsed.scheme in {"http", "https"} and parsed.netloc:
        if any(b in parsed.netloc for b in ("google.", "gstatic.", "ggpht.")):
            return "N/A"
        return raw

    m = re.search(
        r"((?:www\.)?[a-zA-Z0-9][a-zA-Z0-9-]*(?:\.[a-zA-Z0-9-]+)+)(?:/[^\s]*)?",
        raw,
    )
    if m and "google." not in m.group(0):
        return f"https://{m.group(0)}"

    return "N/A"


def _sanitise(text: str) -> str:
    cleaned = re.sub(r"[\ue000-\uf8ff]", "", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "N/A"


async def _handle_popups(page: Page) -> None:
    for sel in [
        'button[aria-label="Accept all"]',
        'button[aria-label="Reject all"]',
        'button[jsname="b3VHJd"]',
        'form[action*="consent"] button',
    ]:
        try:
            btn = await page.wait_for_selector(sel, timeout=2_000)
            if btn:
                await btn.click()
                await asyncio.sleep(0.5)
                return
        except PlaywrightTimeoutError:
            continue


async def _scroll_panel(page: Page, passes: int = 6) -> None:
    for sel in FEED_CSS:
        feed = await page.query_selector(sel)
        if feed:
            for _ in range(passes):
                try:
                    await feed.evaluate("el => el.scrollBy(0, 600)")
                    await asyncio.sleep(0.7)
                except Exception:
                    pass
            logger.info("Scrolled %d×", passes)
            return


def _parse_rating(r: dict) -> float:
    try:
        return float(str(r.get("rating", "0")).replace(",", "."))
    except (ValueError, TypeError):
        return 0.0
