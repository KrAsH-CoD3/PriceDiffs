"""
Forge methodology — JSON-LD first, network API second, DOM fallback.
Powered by CloakBrowser (stealth Chromium).

Phase 1 — Direct HTTP probe for embedded JSON-LD (no browser)
Phase 2 — Open browser, capture network traffic for API discovery
Phase 3 — DOM CSS probe fallback
Phase 4 — Strategy verification and persistence
"""
import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright

CLOAK_BINARY = None
for p in Path.home().joinpath(".cloakbrowser").glob("chromium-*/chrome"):
    CLOAK_BINARY = str(p)
if not CLOAK_BINARY:
    CLOAK_BINARY = ""

STRATEGIES_DIR = Path(__file__).resolve().parent.parent / "data" / "strategies"
MAX_FAILURES_BEFORE_REDISCOVERY = 2

# Proxy for Cloudflare-protected sites (set via env or before calling forge)
PROXY_URL = os.environ.get("PRICEDIFF_PROXY", "")


# ── Helpers ────────────────────────────────────────────────────────────

def get_domain(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")


def _strategy_path(domain: str) -> Path:
    return STRATEGIES_DIR / f"{domain}.json"


def load_strategy(domain: str) -> dict | None:
    path = _strategy_path(domain)
    if path.exists():
        return json.loads(path.read_text())
    return None


def save_strategy(strategy: dict):
    STRATEGIES_DIR.mkdir(parents=True, exist_ok=True)
    _strategy_path(strategy["domain"]).write_text(json.dumps(strategy, indent=2))


def needs_rediscovery(strategy: dict) -> bool:
    return strategy.get("failure_count", 0) > MAX_FAILURES_BEFORE_REDISCOVERY


def _save_meta(strategy: dict):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    if not strategy.get("created_at"):
        strategy["created_at"] = now
    strategy["updated_at"] = now
    save_strategy(strategy)


def mark_success(strategy: dict):
    strategy["success_count"] = strategy.get("success_count", 0) + 1
    strategy["failure_count"] = 0
    _save_meta(strategy)


def mark_failure(strategy: dict):
    strategy["failure_count"] = strategy.get("failure_count", 0) + 1
    _save_meta(strategy)


# ─── CloakBrowser browser — single instance across sessions ─────────────

_browser_instance = None
_playwright_instance = None


async def _get_browser():
    global _browser_instance, _playwright_instance
    if _browser_instance and _browser_instance.is_connected():
        return _browser_instance
    _playwright_instance = await async_playwright().start()
    launch_kwargs = {
        "executable_path": CLOAK_BINARY,
        "headless": True,
        "args": ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
    }
    if PROXY_URL:
        from urllib.parse import urlparse
        parsed = urlparse(PROXY_URL)
        proxy_config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
        if parsed.username:
            proxy_config["username"] = parsed.username
        if parsed.password:
            proxy_config["password"] = parsed.password
        launch_kwargs["proxy"] = proxy_config
    _browser_instance = await _playwright_instance.chromium.launch(**launch_kwargs)
    return _browser_instance


async def _close_browser():
    global _browser_instance, _playwright_instance
    if _browser_instance:
        try:
            await _browser_instance.close()
        except Exception:
            pass
        _browser_instance = None
    if _playwright_instance:
        try:
            await _playwright_instance.stop()
        except Exception:
            pass
        _playwright_instance = None

async def safe_text(resp) -> str:
    """Safely read response body, return empty string on error."""
    try:
        return await resp.text()
    except Exception:
        return ""


# ── Phase 2a — Direct HTTP probe for embedded JSON-LD ──────────────────

async def _try_jsonld_from_http(domain: str, url: str) -> dict | None:
    """Fetch page via plain HTTP and extract JSON-LD structured data.
    
    Tries without proxy first (faster for unprotected sites),
    then retries with proxy if configured.
    """
    for proxy_url in [None, PROXY_URL]:
        try:
            timeout_val = 30 if proxy_url else 15
            kwargs = {"proxy": proxy_url} if proxy_url else {}
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_val, **kwargs) as client:
                resp = await client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                })
                resp.raise_for_status()
                html = resp.text
        except Exception:
            if proxy_url == PROXY_URL:
                return None
            continue

    import re
    ld_pattern = r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>'
    for m in re.finditer(ld_pattern, html, re.DOTALL):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        product = _drill_to_product(data)
        if not product:
            continue
        name = product.get("name", "")
        offers = product.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price_raw = offers.get("price") if isinstance(offers, dict) else None
        if price_raw is None and isinstance(offers, dict):
            spec = offers.get("priceSpecification")
            if isinstance(spec, list):
                price_raw = spec[0].get("price") if spec else None
            elif isinstance(spec, dict):
                price_raw = spec.get("price")
        if not name or price_raw is None:
            continue

        if isinstance(price_raw, str):
            price = float(price_raw.replace(",", ""))
        else:
            price = float(price_raw)
        image = product.get("image", "")
        if isinstance(image, dict) and image.get("@type") == "ImageObject":
            urls = image.get("contentUrl", image.get("url", ""))
            image = urls[0] if isinstance(urls, list) else urls
        if isinstance(image, list):
            image = image[0] if image else ""
        rating_obj = product.get("aggregateRating", {})
        if isinstance(rating_obj, list):
            rating_obj = rating_obj[0] if rating_obj else {}
        rating = rating_obj.get("ratingValue", "")

        return {
            "domain": domain,
            "site_name": domain.split(".")[0].capitalize(),
            "strategy_type": "api",
            "api": {
                "endpoint": url,
                "method": "GET",
                "req_headers": {},
                "field_mapping": {
                    k: ["jsonld", k]
                    for k in ("title", "price", "rating", "image_url")
                },
                "_jsonld_fields": {
                    "title": name,
                    "price": price,
                    "rating": str(rating),
                    "image_url": image if isinstance(image, str) else "",
                },
            },
            "sample_url": url,
            "success_count": 0,
            "failure_count": 0,
        }
    return None


def _drill_to_product(data):
    """Walk JSON-LD to find a Product or mainEntity of type Product."""
    if isinstance(data, dict):
        if data.get("@type") == "Product":
            return data
        if data.get("mainEntity", {}).get("@type") == "Product":
            return data["mainEntity"]
        # @graph format: array of items inside @graph
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                if isinstance(item, dict) and item.get("@type") == "Product":
                    return item
        for val in data.values():
            result = _drill_to_product(val)
            if result:
                return result

async def safe_text(resp) -> str:
    """Safely read response body, return empty string on error."""
    try:
        return await resp.text()
    except Exception:
        return ""


# ── Phase 2a — Direct HTTP probe for embedded JSON-LD ──────────────────

async def _try_jsonld_from_http(domain: str, url: str) -> dict | None:
    """Fetch page via plain HTTP and extract JSON-LD structured data.
    
    Tries without proxy first (faster for unprotected sites),
    then retries with proxy if configured.
    """
    for proxy_url in [None, PROXY_URL]:
        try:
            timeout_val = 30 if proxy_url else 15
            kwargs = {"proxy": proxy_url} if proxy_url else {}
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_val, **kwargs) as client:
                resp = await client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                })
                resp.raise_for_status()
                html = resp.text
        except Exception:
            if proxy_url == PROXY_URL:
                return None
            continue

    import re
    ld_pattern = r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>'
    for m in re.finditer(ld_pattern, html, re.DOTALL):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        product = _drill_to_product(data)
        if not product:
            continue
        name = product.get("name", "")
        offers = product.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price_raw = offers.get("price") if isinstance(offers, dict) else None
        if price_raw is None and isinstance(offers, dict):
            spec = offers.get("priceSpecification")
            if isinstance(spec, list):
                price_raw = spec[0].get("price") if spec else None
            elif isinstance(spec, dict):
                price_raw = spec.get("price")
        if not name or price_raw is None:
            continue

        if isinstance(price_raw, str):
            price = float(price_raw.replace(",", ""))
        else:
            price = float(price_raw)
        image = product.get("image", "")
        if isinstance(image, dict) and image.get("@type") == "ImageObject":
            urls = image.get("contentUrl", image.get("url", ""))
            image = urls[0] if isinstance(urls, list) else urls
        if isinstance(image, list):
            image = image[0] if image else ""
        rating_obj = product.get("aggregateRating", {})
        if isinstance(rating_obj, list):
            rating_obj = rating_obj[0] if rating_obj else {}
        rating = rating_obj.get("ratingValue", "")

        return {
            "domain": domain,
            "site_name": domain.split(".")[0].capitalize(),
            "strategy_type": "api",
            "api": {
                "endpoint": url,
                "method": "GET",
                "req_headers": {},
                "field_mapping": {
                    k: ["jsonld", k]
                    for k in ("title", "price", "rating", "image_url")
                },
                "_jsonld_fields": {
                    "title": name,
                    "price": price,
                    "rating": str(rating),
                    "image_url": image if isinstance(image, str) else "",
                },
            },
            "sample_url": url,
            "success_count": 0,
            "failure_count": 0,
        }
    return None


def _drill_to_product(data):
    """Walk JSON-LD to find a Product or mainEntity of type Product."""
    if isinstance(data, dict):
        if data.get("@type") == "Product":
            return data
        if data.get("mainEntity", {}).get("@type") == "Product":
            return data["mainEntity"]
        # @graph format: array of items inside @graph
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                if isinstance(item, dict) and item.get("@type") == "Product":
                    return item
        for val in data.values():
            result = _drill_to_product(val)
            if result:
                return result
