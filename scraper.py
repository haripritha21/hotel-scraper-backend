"""
Google Maps hotel/restaurant scraper — v3 (restaurant-finder logic merged)

Key improvements from restaurant-finder v2:
  - SETTLE_WAIT increased to 1.8s (was 0.8s) for reliable h1 extraction
  - Junk-name filter expanded to match restaurant-finder's stricter set
  - _normalise_website_url mirrors restaurant-finder's full URL extraction
  - All selector lists updated to match restaurant-finder's battle-tested order
  - goto()-based navigation pattern (no stale element risk)

Hotel-scraper specific features retained:
  - Parallel tab scraping (PARALLEL_TABS = 4) for speed
  - Haversine distance filtering with coordinate extraction from URL
  - Address keyword fallback filter
  - Category allowlist/blocklist (mess/cafe vs lodge/resort)
  - Resource blocking (images/fonts/media) for faster page loads
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
ELEMENT_WAIT  = 10_000   # ms — element wait timeout
PAGE_TIMEOUT  = 60_000   # ms — page navigation timeout (increased for slow networks)
SETTLE_WAIT   = 1.8      # s  — wait after navigation; critical for h1 to stabilise
PARALLEL_TABS = 2        # reduced from 4 — prevents network saturation on slow connections

# ── Junk-name filters (from restaurant-finder v2) ─────────────────────────────
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

# ── CSS selectors (battle-tested order from restaurant-finder) ────────────────
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
    """Return straight-line distance in km between two lat/lon points."""
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def _extract_coords_from_url(url: str) -> tuple[Optional[float], Optional[float]]:
    """
    Pull lat/lon from a Google Maps URL.
    Tries two patterns:
      /@<lat>,<lon>,<zoom>       — map centre after visiting a place
      !3d<lat>!4d<lon>           — embedded in place URL
    """
    m = re.search(r'/@(-?\d+\.\d+),(-?\d+\.\d+)', url)
    if m:
        return float(m.group(1)), float(m.group(2))
    m2 = re.search(r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)', url)
    if m2:
        return float(m2.group(1)), float(m2.group(2))
    return None, None


def _address_matches_location(address: str, location: str) -> bool:
    """
    Returns True if at least ONE word from `location` appears in `address`.
    Short words (≤2 chars) are skipped to avoid false positives.
    """
    addr_lower = address.lower()
    for word in location.lower().split():
        if len(word) > 2 and word in addr_lower:
            return True
    return False


def _geocode_location(location: str) -> tuple[Optional[float], Optional[float]]:
    """
    Convert a location name to lat/lon using Nominatim (OpenStreetMap).
    No API key needed. Returns (lat, lon) or (None, None) on failure.
    Tries progressively shorter versions of the location if full query fails.
    E.g. "thevarsilai,reserveline,satchiyapuram" →
         tries full → "reserveline,satchiyapuram" → "satchiyapuram"
    """
    parts = [p.strip() for p in location.replace("+", " ").split(",")]

    # Try from most-specific (full) to least-specific (last word only)
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

        # ── Block images/fonts/media to load pages faster ─────────────────────
        async def block_heavy_resources(route):
            # Block only binary assets — keep stylesheet so networkidle fires correctly
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
    query_url = (
        f"https://www.google.com/maps/search/"
        f"hotels+restaurants+near+{location.replace(' ', '+')}"
        f"?hl=en"
    )
    logger.info("Opening → %s", query_url)
    await page.goto(query_url, wait_until="networkidle", timeout=PAGE_TIMEOUT)
    await _handle_popups(page)

    # ── Get centre coordinates ────────────────────────────────────────────────
    # Tier 1: Nominatim geocoding — most reliable, works for any location name
    center_lat, center_lon = await asyncio.to_thread(_geocode_location, location)
    if center_lat:
        logger.info("Search centre (geocoded): %.5f, %.5f  radius=%.1f km", center_lat, center_lon, radius_km)

    # Tier 2: search-results page URL (/@lat,lon,zoom pattern)
    if center_lat is None:
        await asyncio.sleep(2)
        current_url = page.url
        center_lat, center_lon = _extract_coords_from_url(current_url)
        if center_lat:
            logger.info("Search centre (page URL): %.5f, %.5f", center_lat, center_lon)
    else:
        await asyncio.sleep(2)  # still wait for page to load

    # ── Wait for feed ─────────────────────────────────────────────────────────
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

    # ── Collect place HREFs (not element references — no stale element risk) ──
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

    # ── Tier 3: extract centre from first result href (!3d!4d pattern) ──────
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

    # ── Parallel scraping: PARALLEL_TABS pages at once ────────────────────────
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

        # ── Junk filter ───────────────────────────────────────────────────────
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

        # ── Category filter ───────────────────────────────────────────────────
        addr     = entry.get("address", "N/A")
        category = entry.get("category", "").lower()
        allowed  = ("restaurant", "mess", "hotel", "cafe", "fast food", "bakery")
        blocked  = ("lodge", "residency", "resort", "inn", "hostel")
        if any(x in category for x in blocked):
            continue
        if category and not any(x in category for x in allowed):
            continue

        # ── Location filter: haversine → address keyword fallback ─────────────
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
            # Have center but no place coords — skip if address doesn't match location
            if addr and addr != "N/A" and not _address_matches_location(addr, location):
                logger.info(
                    "  ✗ SKIP (no place coords + address mismatch) — %s | %s", entry["name"], addr
                )
                continue
        else:
            # No center coords at all — strict address keyword check
            if addr and addr != "N/A" and not _address_matches_location(addr, location):
                logger.info(
                    "  ✗ SKIP (address mismatch) — %s | %s", entry["name"], addr
                )
                continue

        # ── Address deduplication ─────────────────────────────────────────────
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
        await asyncio.sleep(SETTLE_WAIT)   # critical: let dynamic content settle (1.8s)
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
    """Try each CSS selector in order; return first non-empty text found."""
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
    """Try each CSS selector in order; return the first usable external URL."""
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if not el:
                continue
            # Try href first, then aria-label
            for attr in ("href", "aria-label"):
                raw = await el.get_attribute(attr)
                url = _normalise_url(raw)
                if url != "N/A":
                    return url
            # Fallback: text content
            val = await el.text_content()
            url = _normalise_url(val)
            if url != "N/A":
                return url
        except Exception:
            continue

    # Last resort: JS scan for any anchor hinting at 'website' or 'authority'
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
    """Convert Google Maps website values into a clean official URL."""
    if not value:
        return "N/A"
    raw = _sanitise(value).replace("Website:", "", 1).strip()
    if raw == "N/A":
        return "N/A"
    parsed = urlparse(raw)

    # Unwrap Google redirect URLs
    if parsed.netloc and "google." in parsed.netloc:
        params = parse_qs(parsed.query)
        for key in ("q", "url"):
            target = params.get(key, [""])[0]
            if target:
                return _normalise_url(unquote(target))
        return "N/A"

    # Accept clean http/https URLs (not Google CDN)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        if any(b in parsed.netloc for b in ("google.", "gstatic.", "ggpht.")):
            return "N/A"
        return raw

    # Extract bare domain from free-text
    m = re.search(
        r"((?:www\.)?[a-zA-Z0-9][a-zA-Z0-9-]*(?:\.[a-zA-Z0-9-]+)+)(?:/[^\s]*)?",
        raw,
    )
    if m and "google." not in m.group(0):
        return f"https://{m.group(0)}"

    return "N/A"


def _sanitise(text: str) -> str:
    """Remove Google Maps icon glyphs and normalize whitespace."""
    cleaned = re.sub(r"[\ue000-\uf8ff]", "", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "N/A"


async def _handle_popups(page: Page) -> None:
    """Silently dismiss cookie-consent or GDPR dialogs if they appear."""
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
    """Scroll the results feed multiple times to load all lazy-loaded items."""
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
    """Parse rating to float for sorting; invalid → 0."""
    try:
        return float(str(r.get("rating", "0")).replace(",", "."))
    except (ValueError, TypeError):
        return 0.0
