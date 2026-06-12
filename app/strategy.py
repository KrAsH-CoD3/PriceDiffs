"""
Extraction pipeline — per-domain last-success ordering, then fallback.
JSON-LD, metadata, DOM selector probe, and stealth browser (Scrapling + CloakBrowser) for JS-rendered / Cloudflare sites.
Proxy on/off is driven by each domain's last successful configuration.
"""
import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

from dotenv import load_dotenv
from scrapling.fetchers import AsyncFetcher

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

STRATEGIES_DIR = _PROJECT_ROOT / "data" / "strategies"
MAX_FAILURES_BEFORE_REDISCOVERY = 2


def _parse_proxy_raw(raw: str) -> tuple[str, str, str, str] | None:
    if not raw:
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        return None
    parts = raw.rsplit(":", 3)
    if len(parts) == 4:
        host, port, user, pw = parts
        return host, port, user, pw
    return None


def _build_proxy_url(country: str = "us") -> str | None:
    raw = os.environ.get("PRICEDIFF_PROXY", "")
    if not raw:
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    parsed = _parse_proxy_raw(raw)
    if not parsed:
        return None
    host, port, user, pw = parsed
    base_pw = pw.replace("_country-ng", "").replace("_country-us", "")
    return f"http://{user}:{base_pw}_country-{country}@{host}:{port}"


PROXY_URL_NG = _build_proxy_url("ng")
PROXY_URL_US = _build_proxy_url("us")
_disable_proxy = False


def _get_proxy_for_domain(domain: str) -> str | None:
    """Return the appropriate proxy URL for the given domain.
    NG proxy: Jumia only (blocks non-Nigerian IPs).
    US proxy: Amazon/eBay only (curl bypass for captcha + US region)."""
    d = domain.lower()
    if "jumia" in d and (d.endswith(".ng") or d.endswith(".com.ng")):
        return PROXY_URL_NG
    if d in ("jiji.ng",):
        return PROXY_URL_NG
    if d in ("amazon.com", "ebay.com", "m.ebay.com"):
        return PROXY_URL_US
    return None

_fetch_cache: dict[str, object] = {}
_fetch_cache_time: dict[str, float] = {}
_FETCH_CACHE_TTL = 10.0

# Domains that need curl subprocess (HTTPX TLS fingerprint triggers captcha)
_CURL_DOMAINS = {"amazon.com", "ebay.com", "m.ebay.com"}


async def _curl_fetch(url: str, proxy: str | None = None) -> str | None:
    import subprocess
    cmd = ["curl", "-s", "-L", "--connect-timeout", "10", "--max-time", "20"]
    if proxy:
        cmd.extend(["--proxy", proxy])
    cmd.extend([
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
        url,
    ])
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=25)
        if r.returncode == 0 and r.stdout:
            html = r.stdout.decode("utf-8", errors="replace")
            if len(html) > 50000 and "captcha" not in html[:2000].lower():
                return html
        return None
    except Exception:
        return None


def _find_chrome() -> str | None:
    candidates = []
    system = sys.platform
    if system == "linux":
        candidates = [
            "/home/test/.cloakbrowser/chromium-146.0.7680.177.5/chrome",
            "/home/test/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
        ]
    elif system == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "~/.cache/ms-playwright/chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
        ]
    elif system == "win32":
        candidates = [
            os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%PROGRAMFILES(x86)%\Google\Chrome\Application\chrome.exe"),
        ]
    for c in candidates:
        expanded = os.path.expanduser(os.path.expandvars(c))
        if os.path.isfile(expanded):
            return expanded
    if system == "linux":
        for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
            found = shutil.which(name)
            if found:
                return found
    elif system == "darwin":
        found = shutil.which("google-chrome") or shutil.which("chromium")
        if found:
            return found
    return None


# ---------------------------------------------------------------------------
# Stealth browser session (Scrapling + CloakBrowser)
# ---------------------------------------------------------------------------

import platform

_CLOAK_BROWSER_PATH = "/home/test/.cloakbrowser/chromium-146.0.7680.177.5/chrome"
_CHROME_DEPS = "/home/test/.local/lib/chrome-deps"
_BROWSER_TIMEOUT = 30
_OS = platform.system()
_browser_session = None
_browser_broken = False
_browser_fail_count = 0
_vdisplay = None


async def _get_stealth_browser(domain: str):
    global _browser_session, _browser_broken, _browser_fail_count, _vdisplay
    if _browser_broken:
        import time as _t
        _last_browser_attempt = getattr(_get_stealth_browser, "_last_attempt", 0)
        if _t.time() - _last_browser_attempt < 120:
            return None
        _browser_broken = False
        _browser_fail_count = 0
    if _browser_session is not None:
        return _browser_session
    if not os.path.isfile(_CLOAK_BROWSER_PATH):
        _browser_broken = True
        return None
    try:
        from scrapling.fetchers import AsyncStealthySession

        os.environ["LD_LIBRARY_PATH"] = _CHROME_DEPS
        headless = True

        if os.environ.get("PRICEDIFF_HEADED"):
            headless = False
            if _OS == "Linux":
                if not os.environ.get("DISPLAY"):
                    try:
                        from pyvirtualdisplay import Display
                        _vdisplay = Display(size=(1920, 1080), visible=False)
                        _vdisplay.start()
                    except ImportError:
                        headless = True
                    except Exception as e:
                        print(f"  [stealth] Xvfb start failed: {e}")
                        headless = True

        kwargs = dict(
            headless=headless,
            executable_path=_CLOAK_BROWSER_PATH,
            solve_cloudflare=True,
            network_idle=False,
            timeout=20000,
            proxy=None if _disable_proxy else (_get_proxy_for_domain(domain) or None),
            extra_flags=[
                "--disable-blink-features=AutomationControlled",
                "--disable-automation",
            ],
            hide_canvas=True,
            allow_webgl=True,
        )
        _browser_session = AsyncStealthySession(**kwargs)
        await _browser_session.start()
        _browser_fail_count = 0
        return _browser_session
    except Exception as e:
        import time as _t
        _get_stealth_browser._last_attempt = _t.time()
        _browser_broken = True
        return None


async def _close_stealth_browser():
    global _browser_session, _browser_broken, _browser_fail_count, _vdisplay
    if _browser_session is not None:
        try:
            await _browser_session.close()
        except Exception:
            pass
        _browser_session = None
    _browser_broken = False
    _browser_fail_count = 0
    if _vdisplay is not None:
        try:
            _vdisplay.stop()
        except Exception:
            pass
        _vdisplay = None


# ---------------------------------------------------------------------------
# SSR payload extraction (Next.js __NEXT_DATA__, Nuxt __NUXT__, RSC chunks)
# ---------------------------------------------------------------------------

async def _extract_from_ssr_payload(html: str, url: str) -> dict | None:
    """Extract product data from server-rendered JS payloads."""
    domain = get_domain(url)

    # Next.js __NEXT_DATA__
    nd = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.DOTALL)
    if nd:
        try:
            data = json.loads(nd.group(1))
            props = data.get("props", {}).get("pageProps", {})
            product = props.get("product") or props.get("item") or props.get("listing") or {}
            if not product:
                product = _find_product_in_jsonld(props)
            if product:
                title = product.get("name") or product.get("title") or ""
                price_raw = None
                off = product.get("offers", {})
                if isinstance(off, dict):
                    price_raw = off.get("price") or off.get("priceSpecification", {}).get("price")
                elif isinstance(off, list) and off:
                    price_raw = off[0].get("price") or off[0].get("priceSpecification", {}).get("price")
                if not price_raw:
                    price_raw = product.get("price") or product.get("amount") or product.get("priceAmount")
                if isinstance(price_raw, str):
                    price_raw = float(price_raw.replace(",", ""))
                if isinstance(price_raw, (int, float)) and 1.0 < price_raw < 10_000_000_000:
                    image = product.get("image", "")
                    if isinstance(image, list):
                        image = image[0] if image else ""
                    if isinstance(image, dict):
                        image = image.get("url") or image.get("src") or ""
                    if image and not urlparse(image).netloc:
                        image = urljoin(url, image)
                    rating = product.get("aggregateRating", {}).get("ratingValue", "")
                    currency = off.get("priceCurrency", "NGN") if isinstance(off, dict) else "NGN"
                    return {
                        "title": title, "price": float(price_raw),
                        "rating": str(rating), "image_url": image, "currency": currency,
                    }
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

    # Nuxt.js __NUXT__
    nu = re.search(r'<script[^>]*>window\.__NUXT__\s*=\s*(\{.*?\});</script>', html, re.DOTALL)
    if nu:
        try:
            data = json.loads(nu.group(1))
            deep = data
            for key in ("data", "0", "product"):
                if isinstance(deep, dict):
                    deep = deep.get(key, deep)
            product = deep if isinstance(deep, dict) and ("name" in deep or "price" in deep) else None
            if product:
                title = product.get("name") or product.get("title") or ""
                price_raw = product.get("price") or product.get("offers", {}).get("price")
                if isinstance(price_raw, str):
                    price_raw = float(price_raw.replace(",", ""))
                if isinstance(price_raw, (int, float)) and 1.0 < price_raw < 10_000_000_000:
                    image = product.get("image", "")
                    if isinstance(image, list):
                        image = image[0] if image else ""
                    if image and not urlparse(image).netloc:
                        image = urljoin(url, image)
                    return {"title": title, "price": float(price_raw), "rating": "", "image_url": image, "currency": "NGN"}
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

    # React Server Components (RSC) — self.__next_f.push(...)
    rsc = re.findall(r'self\.__next_f\.push\(\[.*?,"(.*?)"\]\)', html, re.DOTALL)
    if rsc:
        combined = "".join(rsc)
        combined = combined.replace("\\\"", "\"").replace("\\n", "").replace("\\\\", "\\")
        # Look for price in the RSC stream
        for pat, group, hint in [
            (r'"(?:price|Price|amount)"\s*:\s*([0-9.]+)', 1, ""),
            (r'>\u20a6\s*([0-9,]+(?:\.[0-9]+)?)<', 1, "ngn"),
            (r'>\$([0-9,]+(?:\.[0-9]+)?)<', 1, "usd"),
        ]:
            m = re.search(pat, combined)
            if m:
                try:
                    v = float(m.group(group).replace(",", ""))
                    if 1.0 < v < 10_000_000_000:
                        title = ""
                        for tpat in [r'"name"\s*:\s*"([^"]+)"', r'"title"\s*:\s*"([^"]+)"', r'<title[^>]*>([^<]+)</title>']:
                            tm = re.search(tpat, combined)
                            if tm:
                                title = tm.group(1)[:120]
                                break
                        if not title:
                            tm = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
                            if tm:
                                title = tm.group(1)[:120]
                        if title:
                            return {"title": title, "price": v, "rating": "", "image_url": "", "currency": "NGN"}
                except (ValueError, IndexError):
                    pass

    return None


async def _try_stealth_fetch(url: str, domain: str | None = None) -> dict | None:
    """Fallback: fetch rendered HTML via CloakBrowser + Scrapling stealth session."""
    global _browser_broken, _browser_fail_count
    if domain is None:
        domain = get_domain(url)
    browser = await _get_stealth_browser(domain)
    if browser is None:
        return None
    try:
        resp = await asyncio.wait_for(browser.fetch(url, network_idle=True, timeout=25000), timeout=_BROWSER_TIMEOUT)
    except asyncio.TimeoutError:
        await _close_stealth_browser()
        return None
    except Exception:
        await _close_stealth_browser()
        return None
    _browser_fail_count = 0
    if resp is None or resp.status >= 400:
        return None
    try:
        html = resp.body.decode(resp.encoding or "utf-8", errors="replace")
    except Exception:
        return None
    if not html or len(html) < 500:
        return None

    domain = get_domain(url)

    # Use the browser-fetched HTML directly instead of re-fetching
    result = await _extract_jsonld(domain, url, html)
    if result:
        fields = result.get("api", {}).get("fields", {})
        p = float(fields.get("price", 0))
        t = fields.get("title", "")
        if t and p > 0:
            return {
                "title": t,
                "price": p,
                "rating": fields.get("rating", ""),
                "image_url": fields.get("image_url", ""),
                "currency": fields.get("currency", "NGN"),
            }

    # Probe Next.js / Nuxt / RSC data payloads
    next_data = await _extract_from_ssr_payload(html, url)
    if next_data:
        return next_data

    result = _parse_metadata_from_html(domain, url, html)
    if result:
        fields = result.get("api", {}).get("fields", {})
        p = float(fields.get("price", 0))
        t = fields.get("title", "")
        if t and p > 0:
            return {
                "title": t,
                "price": p,
                "rating": fields.get("rating", ""),
                "image_url": fields.get("image_url", ""),
                "currency": fields.get("currency", "NGN"),
            }

    strategy = await _probe_dom_selectors(domain, url)
    if strategy:
        data = await _extract_using_dom_strategy(url, strategy)
        if data and _acceptable_data(data):
            return data
    return None


# ---------------------------------------------------------------------------
# Domain & strategy persistence
# ---------------------------------------------------------------------------

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


def _update_timestamps(strategy: dict):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    if not strategy.get("created_at"):
        strategy["created_at"] = now
    strategy["updated_at"] = now
    save_strategy(strategy)


def mark_success(strategy: dict, proxy_on: bool = False, transport: str = "http"):
    strategy["success_count"] = strategy.get("success_count", 0) + 1
    strategy["failure_count"] = 0
    strategy["last_success"] = {"proxy_on": proxy_on, "transport": transport}
    _update_timestamps(strategy)


def mark_failure(strategy: dict):
    strategy["failure_count"] = strategy.get("failure_count", 0) + 1
    _update_timestamps(strategy)


# ---------------------------------------------------------------------------
# HTTP fetch with in-memory cache
# ---------------------------------------------------------------------------

async def _fetch_with_cache(url: str, domain: str | None = None, **kwargs) -> object | None:
    import time
    now = time.monotonic()
    if domain is None:
        domain = get_domain(url)
    proxy = None if _disable_proxy else _get_proxy_for_domain(domain)
    cache_key = f"{url}::proxy={proxy or 'none'}"
    cached = _fetch_cache.get(cache_key)
    cached_at = _fetch_cache_time.get(cache_key, 0)
    if cached is not None and (now - cached_at) < _FETCH_CACHE_TTL:
        return cached

    # For captcha-prone domains, try curl subprocess first (bypasses HTTPX TLS fingerprint)
    if domain in _CURL_DOMAINS:
        html = await _curl_fetch(url, proxy)
        if html:
            from scrapling.engines.toolbelt.custom import Response
            resp = Response(url=url, content=html, status=200, reason="OK",
                            cookies={}, headers={}, request_headers={}, encoding="utf-8")
            _fetch_cache[cache_key] = resp
            _fetch_cache_time[cache_key] = now
            return resp

    for attempt in range(3):
        try:
            resp = await AsyncFetcher.get(url, proxy=proxy or None, **kwargs)
            if resp.status == 429 and attempt < 2:
                await asyncio.sleep(2 ** attempt)
                continue
            if resp.status < 400:
                _fetch_cache[cache_key] = resp
                _fetch_cache_time[cache_key] = now
                return resp
            return resp
        except Exception:
            if attempt < 2:
                await asyncio.sleep(1)
                continue
            return None
    return None


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _decode_response(resp) -> str:
    return resp.body.decode(resp.encoding or "utf-8", errors="replace")


def _strip_scripts(html: str) -> str:
    return re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)


def _first_element(elements: object) -> object | None:
    if elements and len(elements) > 0:
        return elements[0]
    return None


# ---------------------------------------------------------------------------
# JSON-LD extraction
# ---------------------------------------------------------------------------

def _find_product_in_jsonld(data):
    if isinstance(data, dict):
        if data.get("@type") == "Product":
            return data
        if data.get("mainEntity", {}).get("@type") == "Product":
            return data["mainEntity"]
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                if isinstance(item, dict) and item.get("@type") == "Product":
                    return item
        for val in data.values():
            result = _find_product_in_jsonld(val)
            if result:
                return result
    elif isinstance(data, list):
        for item in data:
            result = _find_product_in_jsonld(item)
            if result:
                return result
    return None


def _parse_price_from_text(text: str) -> float | None:
    match = re.search(r"[\u20a6$€£]\s*([0-9,]+(?:\.[0-9]{1,2})?)", text)
    if not match:
        match = re.search(r"([0-9,]+(?:\.[0-9]{1,2})?)\s*[\u20a6$€£]", text)
    if match:
        try:
            v = float(match.group(1).replace(",", ""))
            if 1.0 < v < 10_000_000_000:
                return v
        except ValueError:
            pass
    return None


def _detect_currency(text: str) -> str:
    if "\u20a6" in text or "NGN" in text:
        return "NGN"
    if "\u00a3" in text:
        return "GBP"
    if "\u20ac" in text:
        return "EUR"
    if "$" in text:
        return "USD"
    return "USD"


_CURRENCY_BY_SYMBOL = {"\u20a6": "NGN", "\u00a3": "GBP", "\u20ac": "EUR", "$": "USD"}

_DOMAIN_CURRENCY = {
    "amazon.com": "USD",
    "amazon.co.uk": "GBP",
    "amazon.de": "EUR",
    "amazon.fr": "EUR",
    "amazon.it": "EUR",
    "amazon.es": "EUR",
    "amazon.ca": "CAD",
    "amazon.com.au": "AUD",
    "amazon.co.jp": "JPY",
    "jumia.com.ng": "NGN",
    "konga.com": "NGN",
    "simsng.com": "NGN",
    "agbeke.com": "NGN",
    "slot.ng": "NGN",
    "kara.com.ng": "NGN",
    "ajebomarket.com": "NGN",
}


def _currency_from_match(html: str, match: re.Match, pattern_key: str, domain: str = "") -> str:
    key_map = {"ngn": "NGN", "gbp": "GBP", "eur": "EUR", "usd": "USD"}
    lower = pattern_key.lower()
    for k, v in key_map.items():
        if k in lower:
            return v

    match_text = match.group(0)
    for sym, cur in _CURRENCY_BY_SYMBOL.items():
        if sym in match_text:
            return cur

    start = max(0, match.start() - 800)
    end = min(len(html), match.end() + 200)
    context = html[start:end]
    for sym, cur in _CURRENCY_BY_SYMBOL.items():
        if sym in context:
            return cur

    if domain in _DOMAIN_CURRENCY:
        return _DOMAIN_CURRENCY[domain]
    return _detect_currency(html)


# ---------------------------------------------------------------------------
# PROBE PHASE 1: JSON-LD embedded in HTML
# ---------------------------------------------------------------------------

async def _extract_jsonld(domain: str, url: str, html: str | None = None) -> dict | None:
    if html is None:
        resp = await _fetch_with_cache(url, domain=domain, timeout=30000)
        if resp is None or resp.status >= 400:
            return None
        html = _decode_response(resp)
    if not html:
        return None

    ld_pattern = r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>'
    for m in re.finditer(ld_pattern, html, re.DOTALL):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        product = _find_product_in_jsonld(data)
        if not product:
            continue
        name = product.get("name", "")
        offers = product.get("offers", {})

        def _extract_price_and_currency(offer):
            if not isinstance(offer, dict):
                return None, "NGN"
            ot = offer.get("@type", "")
            if ot == "AggregateOffer":
                low = offer.get("lowPrice") or offer.get("lowprice")
                high = offer.get("highPrice") or offer.get("highprice")
                if low:
                    return low, offer.get("priceCurrency", "USD")
                if not low and not high:
                    return None, "NGN"
                return None, "NGN"
            price = offer.get("price")
            currency = offer.get("priceCurrency", "NGN")
            spec = offer.get("priceSpecification")
            # Prefer sale price from priceSpecification when available
            if price and isinstance(spec, dict):
                spec_type = spec.get("@type", "")
                spec_price = spec.get("price")
                if spec_price and (spec_type == "UnitPriceSpecification" or spec.get("priceType") == "SalePrice"):
                    return spec_price, spec.get("priceCurrency") or currency
            return price, currency

        if isinstance(offers, list):
            best_price = None
            best_currency = "NGN"
            for offer in offers:
                p, c = _extract_price_and_currency(offer)
                if p is not None:
                    try:
                        pv = float(p.replace(",", "")) if isinstance(p, str) else float(p)
                    except (ValueError, TypeError):
                        continue
                    if best_price is None or pv < best_price:
                        best_price = pv
                        best_currency = c
            if best_price is None:
                continue
            price_raw = best_price
            currency_code = best_currency
        else:
            price_raw, currency_code = _extract_price_and_currency(offers)
            if price_raw is None:
                currency_code = "NGN"
                price_raw = None
            if price_raw is None:
                continue
            if isinstance(price_raw, str):
                price_raw_val = float(price_raw.replace(",", ""))
            else:
                price_raw_val = float(price_raw)
            price_raw = price_raw_val

        if not name:
            continue

        if isinstance(price_raw, str):
            price = float(price_raw.replace(",", ""))
        else:
            price = float(price_raw)
        image = product.get("image", "")
        if isinstance(image, dict):
            if image.get("@type") == "ImageObject":
                image = image.get("contentUrl") or image.get("url") or image.get("@id") or ""
            else:
                image = image.get("url") or image.get("contentUrl") or image.get("@id") or ""
        if isinstance(image, list):
            image = image[0] if image else ""
        if isinstance(image, dict):
            image = image.get("contentUrl") or image.get("url") or image.get("@id") or ""
        if isinstance(image, str) and image:
            if not urlparse(image).netloc:
                image = urljoin(url, image)
            if "#" in image:
                image = image.split("#")[0]
        rating_obj = product.get("aggregateRating", {})
        if isinstance(rating_obj, list):
            rating_obj = rating_obj[0] if rating_obj else {}
        rating = rating_obj.get("ratingValue", "")

        return {
            "domain": domain,
            "site_name": domain.split(".")[0].capitalize(),
            "strategy_type": "jsonld",
            "api": {
                "endpoint": url,
                "method": "GET",
                "req_headers": {},
                "field_mapping": {k: ["jsonld", k] for k in ("title", "price", "rating", "image_url")},
                "fields": {
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


# ---------------------------------------------------------------------------
# Image extraction helpers (multiple fallbacks)
# ---------------------------------------------------------------------------

def _extract_image_from_jsonld(html: str, url: str) -> str:
    for m in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        # Walk the JSON tree looking for any "image" field
        stack = [data]
        visited = set()
        while stack:
            node = stack.pop()
            node_id = id(node)
            if node_id in visited:
                continue
            visited.add(node_id)
            if isinstance(node, dict):
                img = node.get("image")
                if img and isinstance(img, str) and img.startswith("http"):
                    if "#" in img:
                        img = img.split("#")[0]
                    return img
                if isinstance(img, dict):
                    val = img.get("contentUrl") or img.get("url") or img.get("@id") or ""
                    if isinstance(val, str) and val.startswith("http"):
                        if "#" in val:
                            val = val.split("#")[0]
                        return val
                if isinstance(img, list):
                    for item in img:
                        if isinstance(item, str) and item.startswith("http"):
                            if "#" in item:
                                item = item.split("#")[0]
                            return item
                        if isinstance(item, dict):
                            val = item.get("contentUrl") or item.get("url") or item.get("@id") or ""
                            if isinstance(val, str) and val.startswith("http"):
                                return val
                for v in node.values():
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(node, list):
                stack.extend(node)
    return ""


def _extract_image_from_embedded_json(html: str) -> str:
    for m in re.finditer(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
        raw = m.group(1).strip()
        if not raw.startswith(("{", "[")):
            continue
        candidates = []
        for key in ("hiRes", "large", "mainUrl", "imageURL"):
            for match in re.finditer(rf'"{key}"\s*:\s*"([^"]+)"', raw):
                val = match.group(1)
                if val.startswith("http") and ("amazon" in val or ".jpg" in val or ".png" in val):
                    candidates.append(val.replace("\\u0026", "&"))
        if candidates:
            return candidates[0]
    return ""


def _extract_image_from_html_attrs(html: str) -> str:
    m = re.search(r'id="landingImage"[^>]+src="([^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'id="imgTagWrapperId"[^>]*>.*?<img[^>]+src="([^"]+)"', html, re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(r'(https://m\.media-amazon\.com/images/I/[^")\s]+(?:\.jpg|\.png|\.webp))', html)
    if m:
        url = m.group(1)
        if "_AC_SL" in url or "_AC_US" in url:
            return url
        url = re.sub(r'\.__AC_[^.]*', '._AC_SL1500_', url)
        return url
    imgs = re.findall(r'<img[^>]+src="(https?://[^"]*\.(?:jpg|jpeg|png|webp|gif)[^"]*)"', html)
    for src in imgs:
        if "logo" not in src.lower() and "icon" not in src.lower() and "banner" not in src.lower():
            return src
    return ""


def _extract_image_from_other_meta(html: str) -> str:
    m = re.search(r'<meta[^>]+name="twitter:image"[^>]+content="([^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'<link[^>]+rel="image_src"[^>]+href="([^"]+)"', html)
    if m:
        return m.group(1)
    return ""


# ---------------------------------------------------------------------------
# PROBE PHASE 2: OG / meta / title tag extraction
# ---------------------------------------------------------------------------

def _extract_title_from_jsonld(html: str) -> str:
    ld_pattern = r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>'
    for m in re.finditer(ld_pattern, html, re.DOTALL):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        product = _find_product_in_jsonld(data)
        if product:
            name = product.get("name", "")
            if name and len(name) > 3:
                return name
    return ""


def _extract_price_from_jsonld(html: str) -> tuple:
    ld_pattern = r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>'
    for m in re.finditer(ld_pattern, html, re.DOTALL):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        product = _find_product_in_jsonld(data)
        if not product:
            continue
        offers = product.get("offers", {})

        def _extract_offer_price(offer):
            if not isinstance(offer, dict):
                return None, ""
            ot = offer.get("@type", "")
            if ot == "AggregateOffer":
                low = offer.get("lowPrice") or offer.get("lowprice")
                if low:
                    return low, offer.get("priceCurrency", "")
                return None, ""
            price_raw = offer.get("price")
            currency = offer.get("priceCurrency", "")
            spec = offer.get("priceSpecification")
            if price_raw and isinstance(spec, dict):
                spec_price = spec.get("price")
                if spec_price and (spec.get("priceType") == "SalePrice" or spec.get("@type") == "UnitPriceSpecification"):
                    return spec_price, spec.get("priceCurrency") or currency
            return price_raw, currency

        if isinstance(offers, list):
            best_price = None
            best_cur = ""
            for offer in offers:
                p, c = _extract_offer_price(offer)
                if p is not None:
                    try:
                        pv = float(p.replace(",", "")) if isinstance(p, str) else float(p)
                    except (ValueError, TypeError):
                        continue
                    if 1.0 < pv < 10_000_000_000 and (best_price is None or pv < best_price):
                        best_price = pv
                        best_cur = c
            if best_price is not None:
                return float(best_price), best_cur
        else:
            price_raw, currency = _extract_offer_price(offers)
            if price_raw is not None:
                if isinstance(price_raw, str):
                    price_raw = float(price_raw.replace(",", ""))
                if isinstance(price_raw, (int, float)) and 1.0 < price_raw < 10_000_000_000:
                    return float(price_raw), currency
    return None, ""


def _looks_like_product_title(text: str, url: str) -> bool:
    """Reject h1 text if it looks like a category/heading, not a product name."""
    if len(text) < 5:
        return False
    url_words = set(re.findall(r'[a-z0-9]+', url.lower().split("/")[-1]))
    # dp/B0FL4HLJ56 — no meaningful words, trust h1
    if not url_words or all(len(w) <= 2 for w in url_words):
        return True
    text_words = set(re.findall(r'[a-z0-9]+', text.lower()))
    # Remove generic store words for overlap check
    generic = {"shop", "store", "online", "shopping", "category", "browse", "home"}
    text_meaningful = text_words - generic
    if len(text_meaningful) < 2:
        return False
    overlap = url_words & text_meaningful
    if len(overlap) >= 2:
        return True
    category_indicators = {"all", "page", "view", "brands", "products", "search", "results"}
    if text_meaningful & category_indicators:
        return False
    return False


def _extract_price_from_rsc(html: str) -> float | None:
    """Extract price from React Server Components (RSC) payloads."""
    rsc = re.findall(r'self\.__next_f\.push\(\[.*?,"(.*?)"\]\)', html, re.DOTALL)
    if not rsc:
        return None
    combined = "".join(rsc).replace("\\\"", "\"").replace("\\n", "").replace("\\\\", "\\")
    for pat in [r'"(?:price|Price|amount)"\s*:\s*"?([0-9.]+)"?', r'>\u20a6\s*([0-9,]+(?:\.[0-9]+)?)<']:
        m = re.search(pat, combined)
        if m:
            try:
                v = float(m.group(1).replace(",", ""))
                if 1.0 < v < 10_000_000_000:
                    return v
            except ValueError:
                pass
    return None


def _parse_metadata_from_html(domain: str, url: str, html: str) -> dict | None:
    title = ""
    og_m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
    if og_m:
        title = og_m.group(1)
    if not title:
        h1_m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.DOTALL)
        if h1_m:
            h1_text = re.sub(r'<[^>]+>', '', h1_m.group(1)).strip()
            if _looks_like_product_title(h1_text, url):
                title = h1_text
    if not title and "Product" in html:
        jld_name = _extract_title_from_jsonld(html)
        if jld_name:
            title = jld_name
    if not title:
        t_m = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL)
        if t_m:
            raw = re.sub(r'<[^>]+>', '', t_m.group(1)).strip()
            raw = re.sub(r'\s*\|.*$', '', raw).strip()
            # Strip common site prefixes like "Amazon.com: "
            raw = re.sub(r'^[A-Za-z0-9.-]+\.[a-z]{2,}:\s*', '', raw).strip()
            title = raw
        else:
            return None
    title = title.replace("&#x20;", " ").replace("&amp;", "&").strip()[:120]
    if not title or "whoops" in title.lower() or "404" in title.lower() or len(title) < 3:
        return None

    price = None
    matched_currency = ""

    og_p = re.search(r'<meta[^>]+property="product:price:amount"[^>]+content="([^"]+)"', html)
    if og_p:
        try:
            price = float(og_p.group(1).replace(",", ""))
            matched_currency = _currency_from_match(html, og_p, "og_price")
        except ValueError:
            price = None

    if not price:
        jld_price, jld_cur = _extract_price_from_jsonld(html)
        if jld_price is not None:
            price = jld_price
            matched_currency = jld_cur

    if not price:
        price_patterns = [
            # Site-specific reliable patterns FIRST
            # eBay: data-testid="x-price" (modern eBay, desktop + mobile)
            (r'data-testid="x-price"[^>]*>\s*\$?\s*([0-9,]+(?:\.[0-9]{1,2})?)', 1, "usd"),
            # eBay: itemprop="price" content="..." microdata
            (r'itemprop="price"[^>]*content="([0-9.,]+)"', 1, "usd"),
            # eBay mobile: class containing display-price / eBay desktop prd-price
            (r'class="[^"]*(?:display-price|prd-price|price-value)[^"]*"[^>]*>\$?\s*([0-9,]+(?:\.[0-9]{1,2})?)', 1, "usd"),
            # eBay: vi-price in JSON payload
            (r'"vi-price"[^}]*"value"\s*:\s*"([0-9.]+)"', 1, "usd"),
            # Amazon: a-price offscreen
            (r'class="[^"]*a-price[^"]*"[^>]*>[\s\S]*?<span[^>]*class="[^"]*a-offscreen[^"]*"[^>]*>\$?\s*([0-9,]+(?:\.[0-9]{1,2})?)', 1, "usd"),
            # NGN / Naira patterns
            (r'>\u20a6\s*([0-9,]+(?:\.[0-9]+)?)<', 1, "ngn"),
            (r'[\u20a6]\s*([0-9,]+(?:\.[0-9]{1,2})?)', 1, "ngn"),
            (r'NGN[\s:]*([0-9,]+(?:\.[0-9]{1,2})?)', 1, "ngn"),
            # GBP, EUR
            (r'\u00a3\s*([0-9,]+(?:\.[0-9]{1,2})?)', 1, "gbp"),
            (r'\u20ac\s*([0-9,]+(?:\.[0-9]{1,2})?)', 1, "eur"),
            # Generic $ pattern
            (r'>\$\s*([0-9,]+(?:\.[0-9]+)?)<', 1, "usd"),
            (r'"priceWithoutCurrencySymbol"\s*:\s*"([0-9.]+)"', 1, ""),
            (r'"amount"\s*:\s*"([0-9.]+)"', 1, ""),
            (r'a-price-whole[^>]*>([0-9,]+)<', 1, ""),
            (r'twitter:data1"\s+value="(?:[A-Z]{3}\s+)?([0-9,]+(?:\.[0-9]{1,2})?)"\s*/?', 1, ""),
            # Generic $ (last resort)
            (r'\$\s*([0-9,]+(?:\.[0-9]{1,2})?)', 1, "usd"),
            (r'[Pp]rice[^<]{0,40}?([0-9,]+(?:\.[0-9]{1,2})?)', 1, ""),
            (r'"(?:price|Price|amount)"\s*:\s*"(?:[A-Z]{3}\s+)?([0-9,]+(?:\.[0-9]{1,2})?)"', 1, ""),
            (r'"(?:price|Price)"\s*:\s*([0-9.]+)', 1, ""),
        ]
        for pat, group, hint in price_patterns:
            m = re.search(pat, html)
            if m:
                try:
                    candidate = float(m.group(group).replace(",", ""))
                    if 1.0 < candidate < 10_000_000_000:
                        price = candidate
                        matched_currency = _currency_from_match(html, m, hint, domain)
                        break
                except (ValueError, IndexError):
                    pass

    if not price:
        p = _extract_price_from_rsc(html)
        if p is not None:
            price = p
            matched_currency = _detect_currency(html)

    if not price:
        return None

    currency = matched_currency or _detect_currency(html)
    image = ""
    og_img = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
    if og_img:
        image = og_img.group(1)
    if image:
        try:
            import urllib.request
            req = urllib.request.Request(image, method="HEAD")
            with urllib.request.urlopen(req, timeout=2) as check:
                if not check.status == 200 or not check.headers.get("Content-Type", "").startswith("image/"):
                    image = ""
        except Exception:
            image = ""
    if not image:
        image = _extract_image_from_jsonld(html, url)
    if not image:
        image = _extract_image_from_embedded_json(html)
    if not image:
        image = _extract_image_from_html_attrs(html)
    if not image:
        image = _extract_image_from_other_meta(html)
    if image and not image.startswith("http"):
        image = urljoin(url, image)

    return {
        "domain": domain,
        "site_name": domain.split(".")[0].capitalize(),
        "strategy_type": "metadata",
        "api": {
            "endpoint": url,
            "method": "GET",
            "req_headers": {},
            "field_mapping": {k: ["jsonld", k] for k in ("title", "price", "rating", "image_url")},
            "fields": {
                "title": title,
                "price": price,
                "rating": "",
                "image_url": image,
                "currency": currency,
            },
        },
        "sample_url": url,
        "success_count": 0,
        "failure_count": 0,
    }


async def _fetch_page_httpx(url: str) -> str | None:
    try:
        import httpx
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            })
            resp.raise_for_status()
            return resp.text
    except Exception:
        return None


async def _extract_metadata(domain: str, url: str) -> dict | None:
    sources = []

    resp = await _fetch_with_cache(url, domain=domain, timeout=15000)
    if resp is not None and resp.status < 400:
        html = _decode_response(resp)
        if html and len(html) >= 500:
            sources.append(html)

    alt_html = await _fetch_page_httpx(url)
    if alt_html and (not sources or alt_html != sources[0]):
        sources.append(alt_html)

    for html in sources:
        result = _parse_metadata_from_html(domain, url, html)
        if result:
            return result
    return None


# ---------------------------------------------------------------------------
# PROBE PHASE 3: DOM selector discovery
# ---------------------------------------------------------------------------

CANDIDATE_SELECTORS = {
    "title": [
        "[data-testid*='title']", "[data-testid*='product-title']",
        ".product-title", ".ProductTitle", ".product-name", ".ProductName",
        "[itemprop='name']", ".headline", ".product-header__title",
        ".page-title", "[class*='product'] h1", "h1",
        ".b-advert-title", ".qa-advert-title", "[class*='advert-title']",
    ],
    "price": [
        "[data-testid*='price']", ".price", ".product-price",
        ".ProductPrice", "[itemprop='price']", ".sale-price", ".regular-price",
        ".price-value", "[class*='price']", ".price-current",
        "[data-automation='product-price']",
        ".qa-advert-price-view-value", ".b-alt-advert-price__text", "[class*='advert-price']",
    ],
    "rating": [
        "[data-testid*='rating']", ".rating", ".star-rating",
        "[itemprop='ratingValue']", ".rating-number", ".stars", "[class*='rating']",
        ".product-rating", ".average-rating",
    ],
    "image": [
        "[data-testid*='image'] img", ".product-image img",
        ".ProductImage img", "[itemprop='image']", ".main-image img",
        ".product-hero img", "[class*='gallery'] img", ".carousel img",
        "img[src*='product']", ".product__image img",
        ".b-slider-image", "[class*='slider-image']",
    ],
}


async def _probe_dom_selectors(domain: str, url: str) -> dict | None:
    resp = await _fetch_with_cache(url, domain=domain, timeout=30000)
    if resp is None or resp.status >= 400:
        return None

    selectors = {}
    for field, css_list in CANDIDATE_SELECTORS.items():
        found = None
        sample = ""
        for css in css_list:
            els = resp.css(css)
            el = _first_element(els)
            if el:
                found = css
                if field == "image":
                    val = el.attrib.get("src") or el.attrib.get("data-src") or ""
                else:
                    val = (el.text or "").strip()
                sample = val[:200]
                break
        selectors[field] = {"css": found, "sample": sample}

    og_title = ""
    og_el = _first_element(resp.css('meta[property="og:title"]'))
    if og_el:
        og_title = og_el.attrib.get("content", "")
    og_image = ""
    og_el = _first_element(resp.css('meta[property="og:image"]'))
    if og_el:
        og_image = og_el.attrib.get("content", "")
    og_price = ""
    og_el = _first_element(resp.css('meta[property="product:price:amount"]'))
    if og_el:
        og_price = og_el.attrib.get("content", "")

    if not selectors["title"]["css"] and og_title:
        selectors["title"] = {"css": None, "sample": og_title, "source": "og:title"}
    if not selectors["image"]["css"] and og_image:
        selectors["image"] = {"css": None, "sample": og_image, "source": "og:image"}
    if not selectors["price"]["css"] and og_price:
        selectors["price"] = {"css": None, "sample": og_price, "source": "og:price"}

    if not any(v.get("css") or v.get("source") for v in selectors.values()):
        return None

    page_title = ""
    t_els = resp.css("title::text")
    if t_els:
        page_title = (t_els.extract_first() or "").strip()

    return {
        "domain": domain,
        "site_name": domain.split(".")[0].capitalize(),
        "strategy_type": "dom",
        "dom": {"selectors": selectors},
        "meta": {
            "page_title": page_title,
            "og_title": og_title or "",
            "og_image": og_image or "",
            "og_price": og_price or "",
        },
        "sample_url": url,
        "success_count": 0,
        "failure_count": 0,
    }


# ---------------------------------------------------------------------------
# Strategy discovery pipeline
# ---------------------------------------------------------------------------

async def discover_extraction_strategy(url: str) -> dict | None:
    domain = get_domain(url)

    strategy = await _extract_jsonld(domain, url)
    if strategy:
        fields = strategy.get("api", {}).get("fields", {})
        price = float(fields.get("price", 0))
        if fields.get("title") and price > 1.0:
            print(f"  [discover] JSON-LD strategy found for {domain}")
            _update_timestamps(strategy)
            return strategy

    strategy = await _extract_metadata(domain, url)
    if strategy:
        fields = strategy.get("api", {}).get("fields", {})
        price = float(fields.get("price", 0))
        if fields.get("title") and price > 1.0:
            print(f"  [discover] Metadata strategy found for {domain}")
            _update_timestamps(strategy)
            return strategy

    print(f"  [discover] Probing DOM selectors for {domain}...")
    strategy = await _probe_dom_selectors(domain, url)
    if strategy and await _verify_extraction(url, strategy):
        _update_timestamps(strategy)
        return strategy

    return None


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# EXTRACTION via saved strategy (api/jsonld type)
# ---------------------------------------------------------------------------

async def _extract_using_jsonld_strategy(url: str, strategy: dict) -> dict | None:
    domain = get_domain(url)
    result = await _extract_jsonld(domain, url)
    if result:
        fields = result.get("api", {}).get("fields", {})
        return {
            "title": fields.get("title", ""),
            "price": float(fields.get("price", 0)),
            "rating": fields.get("rating", ""),
            "image_url": fields.get("image_url", ""),
            "currency": fields.get("currency", "NGN"),
        }
    result = await _extract_metadata(domain, url)
    if result:
        fields = result.get("api", {}).get("fields", {})
        return {
            "title": fields.get("title", ""),
            "price": float(fields.get("price", 0)),
            "rating": fields.get("rating", ""),
            "image_url": fields.get("image_url", ""),
            "currency": fields.get("currency", "NGN"),
        }
    return None


async def _extract_using_metadata_strategy(url: str, strategy: dict) -> dict | None:
    domain = get_domain(url)
    result = await _extract_metadata(domain, url)
    if result:
        fields = result.get("api", {}).get("fields", {})
        return {
            "title": fields.get("title", ""),
            "price": float(fields.get("price", 0)),
            "rating": fields.get("rating", ""),
            "image_url": fields.get("image_url", ""),
            "currency": fields.get("currency", "NGN"),
        }
    return None


async def _extract_using_api_strategy(url: str, strategy: dict) -> dict | None:
    domain = get_domain(url)
    result = await _extract_jsonld(domain, url)
    if result:
        fields = result.get("api", {}).get("fields", {})
        return {
            "title": fields.get("title", ""),
            "price": float(fields.get("price", 0)),
            "rating": fields.get("rating", ""),
            "image_url": fields.get("image_url", ""),
            "currency": fields.get("currency", "NGN"),
        }
    result = await _extract_metadata(domain, url)
    if result:
        fields = result.get("api", {}).get("fields", {})
        return {
            "title": fields.get("title", ""),
            "price": float(fields.get("price", 0)),
            "rating": fields.get("rating", ""),
            "image_url": fields.get("image_url", ""),
            "currency": fields.get("currency", "NGN"),
        }
    return None


# ---------------------------------------------------------------------------
# EXTRACTION via saved DOM strategy
# ---------------------------------------------------------------------------

async def _extract_using_dom_strategy(url: str, strategy: dict) -> dict | None:
    selectors = strategy.get("dom", {}).get("selectors", {})
    resp = await _fetch_with_cache(url, domain=get_domain(url), timeout=30000)
    if resp is None or resp.status >= 400:
        return None
    html = _decode_response(resp)

    title = ""
    sel = selectors.get("title", {}).get("css")
    if sel:
        els = resp.css(sel)
        el = _first_element(els)
        if el:
            title = (el.text or "").strip()
    if not title:
        og_el = _first_element(resp.css('meta[property="og:title"]'))
        if og_el:
            title = og_el.attrib.get("content", "")

    price = 0.0
    sel = selectors.get("price", {}).get("css")
    if sel:
        els = resp.css(sel)
        el = _first_element(els)
        if el:
            pt = (el.text or "").strip()
            parsed = _parse_price_from_text(pt)
            if parsed:
                price = parsed
    if not price:
        og_el = _first_element(resp.css('meta[property="product:price:amount"]'))
        if og_el:
            try:
                price = float(og_el.attrib.get("content", "").replace(",", ""))
            except ValueError:
                pass
    if not price:
        matched = resp.find_by_regex(r"[\u20a6$]\s*([0-9,]+(?:\.[0-9]{1,2})?)")
        if matched and matched.get():
            try:
                price = float(matched.re_first(r"[\u20a6$]\s*([0-9,]+(?:\.[0-9]{1,2})?)").replace(",", ""))
            except (ValueError, AttributeError):
                pass

    rating = ""
    sel = selectors.get("rating", {}).get("css")
    if sel:
        els = resp.css(sel)
        el = _first_element(els)
        if el:
            rt = (el.text or "").strip()
            rm = re.search(r"([\d.]+)\s*out\s*of\s*5", rt)
            if rm:
                rating = rm.group(1)
            elif rt:
                rating = rt[:20]

    image = ""
    sel = selectors.get("image", {}).get("css")
    if sel:
        els = resp.css(sel)
        el = _first_element(els)
        if el:
            image = el.attrib.get("src") or el.attrib.get("data-src") or ""
    if not image:
        og_el = _first_element(resp.css('meta[property="og:image"]'))
        if og_el:
            image = og_el.attrib.get("content", "")
    if image and not image.startswith("http"):
        image = urljoin(url, image)

    currency = _detect_currency(html)

    return {
        "title": title or "",
        "price": price,
        "rating": rating or "",
        "image_url": image or "",
        "currency": currency,
    }


# ---------------------------------------------------------------------------
# Strategy dispatcher
# ---------------------------------------------------------------------------

async def extract_with_strategy(url: str, strategy: dict) -> dict | None:
    strategy_type = strategy.get("strategy_type", "dom")
    if strategy_type == "jsonld":
        return await _extract_using_jsonld_strategy(url, strategy)
    if strategy_type == "metadata":
        return await _extract_using_metadata_strategy(url, strategy)
    if strategy_type == "api":
        return await _extract_using_api_strategy(url, strategy)
    return await _extract_using_dom_strategy(url, strategy)


# ---------------------------------------------------------------------------
# Last-chance extraction (no strategy found)
# ---------------------------------------------------------------------------

async def _fallback_extract(url: str) -> dict | None:
    """Inline JSON-LD then metadata extraction, used when no strategy is available."""
    domain = get_domain(url)

    result = await _extract_jsonld(domain, url)
    if result:
        fields = result.get("api", {}).get("fields", {})
        return {
            "title": fields.get("title", ""),
            "price": float(fields.get("price", 0)),
            "rating": fields.get("rating", ""),
            "image_url": fields.get("image_url", ""),
            "currency": fields.get("currency", "NGN"),
        }

    result = await _extract_metadata(domain, url)
    if result:
        fields = result.get("api", {}).get("fields", {})
        flat = {
            "title": fields.get("title", ""),
            "price": float(fields.get("price", 0)),
            "rating": fields.get("rating", ""),
            "image_url": fields.get("image_url", ""),
            "currency": fields.get("currency", "NGN"),
        }
        if _acceptable_data(flat):
            return flat

    return None


# ---------------------------------------------------------------------------
# Data quality gate
# ---------------------------------------------------------------------------

def _acceptable_data(data) -> bool:
    if not data or not data.get("title") or data.get("price", 0) <= 1.0:
        return False
    title_lower = data["title"].lower()
    if any(w in title_lower for w in ("not found", "whoops", "error", "404", "homepage")):
        return False
    return True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def _scrape_http(url: str) -> dict | None:
    """HTTP extraction via strategy, discovery, or fallback.
    Proxy on/off is controlled by the caller via _disable_proxy."""
    domain = get_domain(url)
    strategy = load_strategy(domain)
    proxy_on = not _disable_proxy

    if strategy and not needs_rediscovery(strategy):
        data = await extract_with_strategy(url, strategy)
        if _acceptable_data(data):
            mark_success(strategy, proxy_on=proxy_on, transport="http")
            return data
        mark_failure(strategy)

    strategy = await discover_extraction_strategy(url)
    if strategy:
        data = await extract_with_strategy(url, strategy)
        if _acceptable_data(data):
            mark_success(strategy, proxy_on=proxy_on, transport="http")
            return data
        mark_failure(strategy)

    data = await _fallback_extract(url)
    if data:
        return data

    return None


def _classify_extraction_failure(resp) -> str:
    if resp is None:
        return "network_error"
    if resp.status in (404, 410):
        return "not_found"
    if resp.status >= 500:
        return "server_error"
    if resp.status == 403:
        html = _decode_response(resp).lower()
        if (
            "cf-browser-verification" in html
            or "__cf_chl_opt" in html
            or "challenge-form" in html
            or "just a moment" in html
        ):
            return "cloudflare"
        return "blocked"
    html = _decode_response(resp).lower()
    if "captcha" in html:
        return "bot_blocked"
    if html_is_unavailable(html):
        return "unavailable"
    return "no_data_parsed"


_GONE_PATTERNS = [
    # General "not found" / "removed"
    "product not found",
    "page not found",
    "this page is no longer active",
    "page you are looking for",
    "this page doesn't exist",
    "we couldn't find this page",
    "this page could not be found",
    "couldn't find the page",
    "could not find the page",
    "we can't find the page",
    "can't find the page",
    "cannot find the page",
    "doesn't exist",
    "does not exist",
    # User-facing "oops" / "sorry"
    "oops",
    "sorry, we couldn't find",
    "sorry, this listing",
    "sorry! we can't find",
    "we are sorry",
    "something went wrong",
    # Sold out / ended
    "sold out",
    "this listing has ended",
    "this listing ended",
    "listing ended",
    "this item is no longer available",
    "item is no longer available",
    "no longer available",
    "this product is no longer",
    "product is no longer",
    "currently unavailable",
    # Removed / taken down
    "product unavailable",
    "item unavailable",
    "listing unavailable",
    "this item was removed",
    "item has been removed",
    "has been removed",
    "has been deleted",
    "listing was removed",
    "advert has been removed",
    "this advert is no longer",
    "advert is no longer",
    # SPA indicators (next.js, nuxt, create-react-app)
    "page-not-found",
    "pagenotfound",
    '"statusCode":404',
    '"status_code":404',
    '"status":404',
    '"statusCode":410',
    'httpErrorCode":404',
    '"notFound":true',
    '"not_found":true',
    # Search / empty results
    "no results found",
    "no products found",
    "0 results",
    "we couldn't find any",
    "couldn't find any",
    "no matching products",
]


def _title_is_gone(title: str) -> bool:
    lowered = title.lower()
    gone_title_patterns = [
        "page not found",
        "not found",
        "404",
        "410",
        "product not found",
        "page not available",
        "page unavailable",
        "oops",
        "listing not found",
        "advert not found",
    ]
    for pattern in gone_title_patterns:
        if pattern in lowered:
            return True
    return False


def html_is_unavailable(html: str) -> bool:
    lowered = html.lower()
    for pattern in _GONE_PATTERNS:
        if pattern in lowered:
            return True
    title_match = re.search(r"<title[^>]*>(.*?)</title>", lowered, re.DOTALL)
    if title_match and _title_is_gone(title_match.group(1)):
        return True
    meta_match = re.search(r'<meta[^>]+name="robots"[^>]+content="([^"]*)"', lowered)
    if meta_match and "noindex" in meta_match.group(1).lower():
        return True
    return False


async def _fetch_and_classify(url: str) -> str:
    resp = await _fetch_with_cache(url, domain=get_domain(url), timeout=8000)
    return _classify_extraction_failure(resp)


def _merge_enrich(base: dict, overlay: dict) -> dict:
    """Fill missing fields in base from overlay without overwriting existing values."""
    result = dict(base)
    for key in ("image_url", "rating"):
        if not result.get(key) and overlay.get(key):
            result[key] = overlay[key]
    return result


# ---------------------------------------------------------------------------
# Jumia fast path — single fetch, direct extraction, no retry onion
# ---------------------------------------------------------------------------

async def _scrape_jumia(url: str) -> dict | None:
    """Dedicated Jumia extraction: one fetch, lower timeout, JSON-LD + metadata fallback."""
    from scrapling.fetchers import AsyncFetcher

    proxy = _get_proxy_for_domain(get_domain(url))
    for attempt in range(2):
        try:
            resp = await AsyncFetcher.get(url, proxy=proxy, timeout=15000)
            break
        except Exception:
            if attempt == 0:
                continue
            return None

    if resp is None or resp.status >= 400:
        return None

    html = _decode_response(resp)
    if not html:
        return None

    domain = get_domain(url)

    # Fast JSON-LD extraction (inline, no separate fetch)
    ld_pat = r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>'
    for m in re.finditer(ld_pat, html, re.DOTALL):
        try:
            ld = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        product = _find_product_in_jsonld(ld)
        if not product:
            continue

        name = product.get("name", "")
        offers = product.get("offers", {})

        def _best_offer_price(offer):
            if not isinstance(offer, dict):
                return None, "NGN"
            ot = offer.get("@type", "")
            if ot == "AggregateOffer":
                low = offer.get("lowPrice") or offer.get("lowprice")
                if low:
                    return low, offer.get("priceCurrency", "NGN")
                return None, "NGN"
            p = offer.get("price")
            c = offer.get("priceCurrency", "NGN")
            spec = offer.get("priceSpecification")
            if p and isinstance(spec, dict) and (spec.get("priceType") == "SalePrice" or spec.get("@type") == "UnitPriceSpecification"):
                return spec.get("price"), spec.get("priceCurrency") or c
            return p, c

        if isinstance(offers, list):
            best_price, best_cur = None, "NGN"
            for offer in offers:
                p, cur = _best_offer_price(offer)
                if p is not None:
                    try:
                        pv = float(p.replace(",", "")) if isinstance(p, str) else float(p)
                    except (ValueError, TypeError):
                        continue
                    if best_price is None or pv < best_price:
                        best_price = pv
                        best_cur = cur
            if best_price is None:
                continue
            price = best_price
            currency = best_cur
        else:
            price_raw, currency = _best_offer_price(offers)
            if price_raw is None:
                continue
            price = float(price_raw.replace(",", "")) if isinstance(price_raw, str) else float(price_raw)

        if not name:
            continue

        image = product.get("image", "")
        if isinstance(image, dict):
            image = image.get("contentUrl") or image.get("url") or ""
        if isinstance(image, list):
            image = image[0] if image else ""
        if isinstance(image, str) and image:
            if not urlparse(image).netloc:
                image = urljoin(url, image)

        rating_obj = product.get("aggregateRating", {})
        if isinstance(rating_obj, list):
            rating_obj = rating_obj[0] if rating_obj else {}
        rating = rating_obj.get("ratingValue", "")

        result = {"title": name, "price": price, "currency": currency, "image_url": image, "rating": rating}
        if result.get("title") and result.get("price", 0) > 0:
            return result

    # Fallback: metadata extraction on same HTML
    meta = _parse_metadata_from_html(domain, url, html)
    if meta:
        fields = meta.get("api", {}).get("fields", {})
        if fields.get("title") and fields.get("price", 0) > 1.0:
            return {
                "title": fields.get("title", ""),
                "price": float(fields.get("price", 0)),
                "rating": fields.get("rating", ""),
                "image_url": fields.get("image_url", ""),
                "currency": fields.get("currency", "NGN"),
            }

    return None


async def _scrape_jiji(url: str) -> dict | None:
    """Dedicated Jiji extraction: one fetch, lower timeout, JSON-LD + metadata fallback."""
    from scrapling.fetchers import AsyncFetcher

    proxy = _get_proxy_for_domain(get_domain(url))
    for attempt in range(2):
        try:
            resp = await AsyncFetcher.get(url, proxy=proxy, timeout=15000)
            break
        except Exception:
            if attempt == 0:
                continue
            return None

    if resp is None or resp.status >= 400:
        return None

    html = _decode_response(resp)
    if not html:
        return None

    domain = get_domain(url)

    ld_pat = r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>'
    for m in re.finditer(ld_pat, html, re.DOTALL):
        try:
            ld = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        product = _find_product_in_jsonld(ld)
        if not product:
            continue

        name = product.get("name", "")
        offers = product.get("offers", {})

        def _best_offer_price(offer):
            if not isinstance(offer, dict):
                return None, "NGN"
            ot = offer.get("@type", "")
            if ot == "AggregateOffer":
                low = offer.get("lowPrice") or offer.get("lowprice")
                if low:
                    return low, offer.get("priceCurrency", "NGN")
                return None, "NGN"
            p = offer.get("price")
            c = offer.get("priceCurrency", "NGN")
            spec = offer.get("priceSpecification")
            if p and isinstance(spec, dict) and (spec.get("priceType") == "SalePrice" or spec.get("@type") == "UnitPriceSpecification"):
                return spec.get("price"), spec.get("priceCurrency") or c
            return p, c

        if isinstance(offers, list):
            best_price, best_cur = None, "NGN"
            for offer in offers:
                p, cur = _best_offer_price(offer)
                if p is not None:
                    try:
                        pv = float(p.replace(",", "")) if isinstance(p, str) else float(p)
                    except (ValueError, TypeError):
                        continue
                    if best_price is None or pv < best_price:
                        best_price = pv
                        best_cur = cur
            if best_price is None:
                continue
            price = best_price
            currency = best_cur
        else:
            price_raw, currency = _best_offer_price(offers)
            if price_raw is None:
                continue
            price = float(price_raw.replace(",", "")) if isinstance(price_raw, str) else float(price_raw)

        if not name:
            continue

        image = product.get("image", "")
        if isinstance(image, dict):
            image = image.get("contentUrl") or image.get("url") or ""
        if isinstance(image, list):
            image = image[0] if image else ""
        if isinstance(image, str) and image:
            if not urlparse(image).netloc:
                image = urljoin(url, image)
        image = product.get("image", "")
        if isinstance(image, dict):
            image = image.get("contentUrl") or image.get("url") or ""
        if isinstance(image, list):
            image = image[0] if image else ""
        if isinstance(image, str) and image:
            if not urlparse(image).netloc:
                image = urljoin(url, image)

        rating_obj = product.get("aggregateRating", {})
        if isinstance(rating_obj, list):
            rating_obj = rating_obj[0] if rating_obj else {}
        rating = rating_obj.get("ratingValue", "")

        result = {"title": name, "price": price, "currency": currency, "image_url": image, "rating": rating}
        if result.get("title") and result.get("price", 0) > 0:
            return result

    meta = _parse_metadata_from_html(domain, url, html)
    if meta:
        fields = meta.get("api", {}).get("fields", {})
        if fields.get("title") and fields.get("price", 0) > 1.0:
            return {
                "title": fields.get("title", ""),
                "price": float(fields.get("price", 0)),
                "rating": fields.get("rating", ""),
                "image_url": fields.get("image_url", ""),
                "currency": fields.get("currency", "NGN"),
            }

    return None


async def _try_phase(url: str, proxy_on: bool, transport: str, accumulated: dict | None) -> dict | None:
    """Execute a single scrape phase. Returns data or None."""
    global _disable_proxy
    domain = get_domain(url)
    d0 = _disable_proxy
    _disable_proxy = not proxy_on
    try:
        if transport == "http":
            result = await _scrape_http(url)
            if result:
                result = _merge_enrich(result, accumulated or {})
                return result
            return None

        # transport == "browser"
        result = await _try_stealth_fetch(url, domain)
        if result:
            result = _merge_enrich(result, accumulated or {})
            if result.get("title") and result.get("price", 0) > 0:
                strat = load_strategy(domain)
                if strat:
                    mark_success(strat, proxy_on=proxy_on, transport="browser")
                return result
        return None
    finally:
        _disable_proxy = d0


def _build_phase_order(strategy: dict | None) -> list[dict]:
    """Return scrape phases ordered by last success, then default as fallback."""
    phases = [
        {"proxy_on": False, "transport": "http", "label": "http-no-proxy"},
        {"proxy_on": True, "transport": "http", "label": "http-with-proxy"},
        {"proxy_on": False, "transport": "browser", "label": "browser-no-proxy"},
        {"proxy_on": True, "transport": "browser", "label": "browser-with-proxy"},
    ]
    last = (strategy or {}).get("last_success", {})
    if last:
        preferred = {"proxy_on": last.get("proxy_on", False), "transport": last.get("transport", "http")}
        for i, p in enumerate(phases):
            if p["proxy_on"] == preferred["proxy_on"] and p["transport"] == preferred["transport"]:
                phases.insert(0, phases.pop(i))
                break
    return phases


async def scrape_url(url: str) -> dict | None:
    global _disable_proxy

    # Rewrite eBay desktop URLs to mobile (bypasses bot challenge)
    parsed = urlparse(url)
    if parsed.netloc in ("www.ebay.com", "ebay.com", "ebay.co.uk", "www.ebay.co.uk"):
        url = urlunparse(parsed._replace(netloc="m." + parsed.netloc.removeprefix("www.")))

    d0 = _disable_proxy
    domain = get_domain(url)

    # Jiji and Jumia work best with proxy — dedicated fast path, single fetch + direct extraction
    if _get_proxy_for_domain(domain) and ("jiji" in parsed.netloc or "jumia" in parsed.netloc):
        _disable_proxy = False
        try:
            if "jiji" in parsed.netloc:
                data = await _scrape_jiji(url)
            else:
                data = await _scrape_jumia(url)
        finally:
            _disable_proxy = d0
        if data:
            data["_proxy"] = True
            data["_strategy"] = f"{'jiji' if 'jiji' in parsed.netloc else 'jumia'}-via-proxy"
            strat = load_strategy(domain)
            if strat:
                mark_success(strat, proxy_on=True, transport="http")
            return data
        return None

    strategy = load_strategy(domain)
    phase_order = _build_phase_order(strategy)

    has_proxy = bool(_get_proxy_for_domain(domain))
    accumulated = None
    cls = None

    for phase in phase_order:
        p_on = phase["proxy_on"]
        t = phase["transport"]

        # Skip proxy phases when no proxy is configured
        if p_on and not has_proxy:
            continue

        # Skip browser phases for bot-blocked sites
        if t == "browser" and cls == "bot_blocked":
            continue

        # Classify failure before browser phases
        if t == "browser" and cls is None and accumulated is None:
            cls = await _fetch_and_classify(url)
            if cls == "not_found":
                if accumulated:
                    accumulated["_proxy"] = False
                    accumulated["_strategy"] = "partial-http-no-proxy"
                return accumulated if accumulated else None

        result = await _try_phase(url, p_on, t, accumulated)
        if result:
            result["_proxy"] = p_on
            result["_strategy"] = phase["label"]
            # HTTP results need image_url to be considered complete
            if t == "http" and not result.get("image_url"):
                accumulated = result if accumulated is None else _merge_enrich(result, accumulated)
                continue
            return result

    if accumulated:
        accumulated["_proxy"] = False
        accumulated["_strategy"] = "partial-http-no-proxy"
    return accumulated


# ---------------------------------------------------------------------------
# Strategy verification (used internally after DOM probe)
# ---------------------------------------------------------------------------

async def _verify_extraction(url: str, strategy: dict) -> bool:
    data = await extract_with_strategy(url, strategy)
    if data is None:
        return False
    if not data.get("title") or len(data["title"]) < 3:
        return False
    if not data.get("price", 0) > 0:
        return False
    return True
