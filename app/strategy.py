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
    try:
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
    except Exception:
        _browser_instance = None
        return None


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


# ═══════════════════════════════════════════════════════════════════════
# PHASE 2 — CAPABILITY EXPLORATION
# Priority: API endpoints → JS runtime → DOM
# ═══════════════════════════════════════════════════════════════════════

async def forge_strategy(url: str) -> dict | None:
    """Full Phase 2 → Phase 3 workflow powered by CloakBrowser.

    1. Probe page with direct HTTP for embedded JSON-LD (fast path)
    2. Open page with CloakBrowser, capture network traffic
    3. Inspect XHR/fetch responses for product data APIs
    4. If no API → DOM probe → save DOM strategy
    5. Verify strategy works before returning
    """
    domain = get_domain(url)

    # ── Phase 2a: Direct HTTP probe for JSON-LD (no browser needed) ──
    strategy = await _try_jsonld_from_http(domain, url)
    if strategy:
        print(f"  [forge] JSON-LD strategy found for {domain} via HTTP")
        _save_meta(strategy)
        return strategy

    # ── Phase 2b: Browser-based exploration ──────────────────────────
    browser = await _get_browser()
    if not browser:
        print(f"  [forge] Browser unavailable, skipping {domain}")
        return None
    page = await browser.new_page()
    responses = []

    async def on_response(resp):
        responses.append({
            "url": resp.url,
            "status": resp.status,
            "method": resp.request.method,
            "headers": resp.headers,
            "request_headers": resp.request.headers,
            "body": await safe_text(resp),
        })

    page.on("response", on_response)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)
    except Exception as e:
        print(f"  [forge] Page load error: {e}")
        await page.close()
        return None

    # ── Phase 2c: API endpoint discovery ─────────────────────────────
    strategy = _discover_api_from_responses(domain, url, responses)
    if strategy:
        print(f"  [forge] API strategy found for {domain}, verifying...")
        await page.close()
        if await _verify_strategy(url, strategy):
            _save_meta(strategy)
            return strategy
        print(f"  [forge] API verification failed, falling back to DOM")
        # Re-create page (was closed during verify) and navigate
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2000)
        except Exception:
            await page.close()
            return None

    # ── Phase 2d: DOM fallback — probe CSS selectors ─────────────────
    print(f"  [forge] Probing DOM selectors for {domain}...")
    strategy = await _discover_dom(domain, url, page)
    await page.close()
    if strategy and await _verify_strategy(url, strategy):
        _save_meta(strategy)
        return strategy

    return None


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
    proxy_candidates = [None]
    if PROXY_URL:
        proxy_candidates.append(PROXY_URL)
    html = None
    for proxy_url in proxy_candidates:
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
    if html is None:
        return None

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
        currency_code = "NGN"
        if isinstance(offers, dict):
            currency_code = offers.get("priceCurrency", "NGN")
        elif isinstance(offers, list) and offers:
            currency_code = offers[0].get("priceCurrency", "NGN")
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
                    "currency": currency_code,
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
    elif isinstance(data, list):
        for item in data:
            result = _drill_to_product(item)
            if result:
                return result
    return None


# ── Phase 2b — API discovery ───────────────────────────────────────────

def _discover_api_from_responses(domain: str, url: str,
                                 responses: list) -> dict | None:
    """Inspect captured network responses for product data APIs.

    Follows forge skill's 'Fetch once, analyze many' rule.
    """
    candidates = []

    for resp in responses:
        body = resp.get("body", "")
        if not _looks_like_product_json(body):
            continue

        mapping = _map_fields_from_json(body)
        if not mapping or not mapping.get("title") or not mapping.get("price"):
            continue

        candidates.append({
            "endpoint": resp["url"],
            "method": resp["method"],
            "req_headers": {
                k: v for k, v in resp.get("request_headers", {}).items()
                if k.lower() in (
                    "accept", "content-type", "referer", "origin",
                    "x-csrf-token", "authorization", "x-api-key",
                    "x-requested-with",
                )
            },
            "field_mapping": mapping,
            "confidence": _score_mapping(mapping),
        })

    if not candidates:
        return None

    best = max(candidates, key=lambda c: c["confidence"])
    return {
        "domain": domain,
        "site_name": domain.split(".")[0].capitalize(),
        "strategy_type": "api",
        "api": {
            "endpoint": best["endpoint"],
            "method": best["method"],
            "req_headers": best["req_headers"],
            "field_mapping": best["field_mapping"],
        },
        "sample_url": url,
        "success_count": 0,
        "failure_count": 0,
    }


def _looks_like_product_json(body: str) -> bool:
    body = body.strip()
    if not body.startswith(("{", "[")):
        return False
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return False
    text = json.dumps(data).lower()
    title_keys = ("title", "name", "product_name", "item_name", "heading")
    price_keys = ("price", "sale_price", "regular_price", "current_price",
                  "pricing", "amount", "cost")
    return any(k in text for k in title_keys) and any(k in text for k in price_keys)


def _map_fields_from_json(body: str) -> dict | None:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if isinstance(data, list) and data:
        data = data[0]

    mapping = {}
    for field, keys in (
        ("title", ("title", "name", "product_name", "item_name", "heading")),
        ("price", ("price", "sale_price", "regular_price", "current_price", "amount")),
        ("rating", ("rating", "average_rating", "review_rating", "star_rating")),
        ("image_url", ("image", "images", "image_url", "thumbnail", "picture", "img")),
    ):
        path = _find_in_json(data, keys)
        if path:
            mapping[field] = _path_to_list(path)

    if "title" in mapping and "price" in mapping:
        return mapping
    return None


def _find_in_json(obj, keys: tuple, path: tuple = ()) -> tuple | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = k.lower()
            if any(key in kl or kl in key for key in keys):
                if isinstance(v, (str, int, float)):
                    return path + (k,)
                if isinstance(v, dict):
                    for ck in ("value", "amount", "raw", "display", "text", "current"):
                        if ck in v:
                            return path + (k, ck)
                    fv = next((vk for vk in v.values()
                               if isinstance(vk, (str, int, float))), None)
                    if fv is not None:
                        return path + (k, next(vk for vk, vv in v.items() if vv is fv))
                    return path + (k,)
            result = _find_in_json(v, keys, path + (k,))
            if result:
                return result
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            result = _find_in_json(item, keys, path + (str(i),))
            if result:
                return result
    return None


def _path_to_list(path: tuple) -> list:
    return list(path)


def _score_mapping(mapping: dict) -> int:
    score = 0
    if mapping.get("title"):
        score += 3
    if mapping.get("price"):
        score += 3
    if mapping.get("rating"):
        score += 1
    if mapping.get("image_url"):
        score += 1
    return score


# ── Phase 2c — DOM discovery (fallback) ────────────────────────────────

async def _discover_dom(domain: str, url: str, page) -> dict | None:
    """DOM-based fallback: probe CSS selectors and OG meta tags."""
    probe_js = r"""
(() => {
    const candidates = {
        title: [
            "#productTitle", "h1", "[data-testid*='title']", "[data-testid*='product-title']",
            ".product-title", ".ProductTitle", ".product-name", ".ProductName",
            "[itemprop='name']", ".title", ".headline", ".product-header__title",
            "h1 span", ".page-title", "[class*='product'] h1",
            ".b-advert-title", ".qa-advert-title", "[class*='advert-title']"
        ],
        price: [
            ".a-price .a-offscreen", "[data-testid*='price']", ".price", ".product-price",
            ".ProductPrice", "[itemprop='price']", ".sale-price", ".regular-price",
            ".price-value", "[class*='price']", ".a-price-whole",
            "[data-automation='product-price']", ".price-current",
            ".qa-advert-price-view-value", ".b-alt-advert-price__text", "[class*='advert-price']"
        ],
        rating: [
            "#acrPopover", "[data-testid*='rating']", ".rating", ".star-rating",
            "[itemprop='ratingValue']", ".rating-number", ".stars", "[class*='rating']",
            ".product-rating", ".average-rating"
        ],
        image: [
            "#landingImage", "[data-testid*='image'] img", ".product-image img",
            ".ProductImage img", "[itemprop='image']", ".main-image img",
            ".product-hero img", "[class*='gallery'] img", ".carousel img",
            "img[src*='product']", ".product__image img",
            ".b-slider-image", "[class*='slider-image']"
        ]
    };
    const results = {};
    for (const [field, selectors] of Object.entries(candidates)) {
        for (const sel of selectors) {
            try {
                const el = document.querySelector(sel);
                if (!el) continue;
                const text = (el.textContent || el.innerText || "").trim();
                const src = el.getAttribute("src") || el.getAttribute("data-src") || "";
                const alt = el.getAttribute("alt") || "";
                const href = el.getAttribute("href") || "";
                results[field] = {
                    selector: sel, text: text.slice(0, 300), src: src.slice(0, 500),
                    alt: alt.slice(0, 100), href: href.slice(0, 500),
                    tag: el.tagName.toLowerCase(),
                    is_visible: !!(el.offsetParent || el.offsetWidth || el.offsetHeight)
                };
                break;
            } catch(e) { continue; }
        }
        if (!results[field]) results[field] = { selector: null, text: "", src: "" };
    }
    results._html_title = document.title;
    results._meta_desc = (document.querySelector("meta[name='description']") || {}).content || "";
    results._og_image = (document.querySelector("meta[property='og:image']") || {}).content || "";
    results._og_title = (document.querySelector("meta[property='og:title']") || {}).content || "";
    results._og_price = (document.querySelector("meta[property='product:price:amount']") || {}).content || "";
    return JSON.stringify(results);
})();
"""
    try:
        raw = await page.evaluate(probe_js)
    except Exception:
        return None

    if not raw or not raw.startswith("{"):
        return None
    try:
        probe = json.loads(raw)
    except json.JSONDecodeError:
        return None

    selectors = {}
    for field in ("title", "price", "rating", "image"):
        info = probe.get(field) or {}
        sel = info.get("selector")
        text = info.get("text", "")
        src = info.get("src", "") or info.get("href", "")
        selectors[field] = {"css": sel, "sample": (text or src or "")[:200]}

    og_title = probe.get("_og_title", "")
    og_image = probe.get("_og_image", "")
    og_price = probe.get("_og_price", "")

    if not selectors["title"]["css"] and og_title:
        selectors["title"] = {"css": None, "sample": og_title, "source": "og:title"}
    if not selectors["image"]["css"] and og_image:
        selectors["image"] = {"css": None, "sample": og_image, "source": "og:image"}
    if not selectors["price"]["css"] and og_price:
        selectors["price"] = {"css": None, "sample": og_price, "source": "og:price"}

    if not any(v.get("css") or v.get("source") for v in selectors.values()):
        return None

    return {
        "domain": domain,
        "site_name": domain.split(".")[0].capitalize(),
        "strategy_type": "dom",
        "dom": {"selectors": selectors},
        "meta": {
            "page_title": probe.get("_html_title", ""),
            "og_title": og_title,
            "og_image": og_image,
            "og_price": og_price,
        },
        "sample_url": url,
        "success_count": 0,
        "failure_count": 0,
    }


# ═══════════════════════════════════════════════════════════════════════
# EXTRACTION
# ═══════════════════════════════════════════════════════════════════════

def _get_field(data: dict | list, path: list):
    current = data
    for key in path:
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, list):
            try:
                idx = int(key)
                current = current[idx] if 0 <= idx < len(current) else None
            except (ValueError, IndexError):
                return None
        else:
            return None
        if current is None:
            return None
    return current


async def extract_with_strategy(url: str, strategy: dict) -> dict | None:
    strategy_type = strategy.get("strategy_type", "dom")
    if strategy_type == "api":
        return await _extract_via_api(url, strategy)
    return await _extract_via_dom(url, strategy)


async def _extract_via_api(url: str, strategy: dict) -> dict | None:
    api = strategy.get("api", {})
    endpoint = api.get("endpoint", "")
    method = api.get("method", "GET").upper()
    headers = api.get("req_headers", {})
    mapping = api.get("field_mapping", {})

    # JSON-LD strategy: always extract from the actual URL (domain-wide, not URL-locked)
    jsonld_fields = api.get("_jsonld_fields")
    if jsonld_fields is not None:
        domain = get_domain(url)
        result = await _try_jsonld_from_http(domain, url)
        if result:
            fields = result.get("api", {}).get("_jsonld_fields", {})
            return {
                "title": fields.get("title", ""),
                "price": float(fields.get("price", 0)),
                "rating": fields.get("rating", ""),
                "image_url": fields.get("image_url", ""),
                "currency": fields.get("currency", "NGN"),
            }
        return None

    try:
        kwargs = {"proxy": PROXY_URL} if PROXY_URL else {}
        async with httpx.AsyncClient(follow_redirects=True, timeout=30, **kwargs) as client:
            if method == "GET":
                resp = await client.get(endpoint, headers=headers)
            elif method == "POST":
                resp = await client.post(endpoint, headers=headers)
            else:
                return None
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None

    result = {}
    for field, path in mapping.items():
        val = _get_field(data, path)
        result[field] = str(val).strip() if val is not None else ""

    price_val = 0.0
    try:
        cleaned = re.sub(r"[^\d.,]", "", result.get("price", "0").replace(",", "."))
        cleaned = cleaned.replace(".", "", cleaned.count(".") - 1) if cleaned.count(".") > 1 else cleaned
        price_val = float(cleaned) if cleaned else 0.0
    except (ValueError, TypeError):
        pass

    return {
        "title": result.get("title", ""),
        "price": price_val,
        "rating": result.get("rating", ""),
        "image_url": result.get("image_url", ""),
        "currency": "NGN",
    }


async def _extract_via_dom(url: str, strategy: dict) -> dict | None:
    browser = await _get_browser()
    page = await browser.new_page()

    selectors = strategy.get("dom", {}).get("selectors", {})
    js = _build_dom_extract_js(selectors)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)
        raw = await page.evaluate(js)
    except Exception:
        return None
    finally:
        await page.close()

    if not raw or not raw.startswith("{"):
        return None
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return None

    price_val = 0.0
    try:
        price_str = (result.get("price") or "0").replace(",", "")
        price_val = float(price_str) if price_str else 0.0
    except (ValueError, TypeError):
        pass

    return {
        "title": (result.get("title") or "").strip(),
        "price": price_val,
        "rating": (result.get("rating") or "").strip(),
        "image_url": (result.get("image_url") or "").strip(),
        "currency": result.get("currency") or "NGN",
    }


def _build_dom_extract_js(selectors: dict) -> str:
    parts = []
    var_map = {
        "title": ("$t", "title", False),
        "price": ("$p", "priceText", False),
        "rating": ("$r", "ratingText", False),
        "image": ("$i", "imageSrc", True),
    }
    for field, (sv, rv, is_attr) in var_map.items():
        info = selectors.get(field, {})
        css = info.get("css") or (info if isinstance(info, str) else "")
        if css:
            safe_css = css.replace("'", "\\'")
            parts.append(f"const {sv} = document.querySelector('{safe_css}')")
            if is_attr:
                parts.append(
                    f"""{rv} = ({sv} ? ({sv}.getAttribute('src')||{sv}.getAttribute('data-src')||{sv}.getAttribute('content')||'') : '')""")
            else:
                parts.append(
                    f"{rv} = ({sv} ? ({sv}.textContent||{sv}.innerText||'').trim() : '')")
        else:
            parts.append(f"{rv} = ''")

    js = ";\n".join(parts) + ";\n" + """
var match1 = priceText.match(/\\u20A6\\s*([0-9,]+)/);
var match2 = priceText.match(/\\$\\s*([0-9,]+)/);
var match3 = priceText.match(/([0-9,]+)\\s*\\u20A6/);
var match4 = priceText.match(/([0-9,]+)\\s*\\$/);
var priceMatch = match1 || match2 || match3 || match4;
var currencyCode = match1 || match3 ? 'NGN' : (priceMatch ? 'USD' : '');
var ratingMatch = ratingText.match(/([\\d.]+)\\s*out\\s*of\\s*5/i);
var ogPrice = (document.querySelector("meta[property='product:price:amount']") || {}).content || "";
var ogImage = (document.querySelector("meta[property='og:image']") || {}).content || "";
var ogTitle = (document.querySelector("meta[property='og:title']") || {}).content || "";
if (!title && ogTitle) title = ogTitle;
if (!imageSrc && ogImage) imageSrc = ogImage;
if (!priceMatch && ogPrice) { priceText = ogPrice; match1 = ogPrice.match(/\\u20A6\\s*([0-9,]+)/); match2 = ogPrice.match(/\\$\\s*([0-9,]+)/); match3 = ogPrice.match(/([0-9,]+)\\s*\\u20A6/); match4 = ogPrice.match(/([0-9,]+)\\s*\\$/); priceMatch = match1 || match2 || match3 || match4; currencyCode = match1 || match3 ? 'NGN' : (priceMatch ? 'USD' : ''); }
var priceStr = priceMatch ? priceMatch[1].replace(/,/g, "") : "0";
JSON.stringify({
    title: title || "",
    price: priceStr,
    rating: ratingMatch ? ratingMatch[1] : "",
    image_url: (imageSrc || "").startsWith("http") ? imageSrc : (imageSrc ? new URL(imageSrc, document.baseURI).href : ""),
    currency: currencyCode
});
"""
    return js


# ═══════════════════════════════════════════════════════════════════════
# SINGLE-URL SCRAPE (shared by views, scrape command, and scheduler)
# ═══════════════════════════════════════════════════════════════════════

async def scrape_url(url: str) -> dict | None:
    """Scrape a single product URL end-to-end.
    
    Loads cached strategy or forges a new one, extracts data.
    Returns dict with keys: title, price, rating, image_url, currency
    or None if all attempts failed.
    """
    domain = get_domain(url)
    strategy = load_strategy(domain)

    if strategy and not needs_rediscovery(strategy):
        data = await extract_with_strategy(url, strategy)
        if data and data.get("title") and data.get("price", 0) > 0:
            mark_success(strategy)
            return data
        mark_failure(strategy)

    strategy = await forge_strategy(url)
    if not strategy:
        return None
    mark_success(strategy)
    return await extract_with_strategy(url, strategy)


# ═══════════════════════════════════════════════════════════════════════
# VERIFICATION (Phase 3 — Delivery step)
# ═══════════════════════════════════════════════════════════════════════

async def _verify_strategy(url: str, strategy: dict) -> bool:
    data = await extract_with_strategy(url, strategy)
    if data is None:
        return False
    if not data.get("title") or len(data["title"]) < 3:
        return False
    if not data.get("price", 0) > 0:
        return False
    return True
