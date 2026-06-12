"""Retry all failed sites with better URLs and direct HTTP extraction."""
import asyncio
import json
import re
from urllib.parse import urlparse
from pathlib import Path

import httpx
from django.core.management.base import BaseCommand
from app.strategy import (
    scrape_url,
    get_domain, load_strategy, save_strategy, STRATEGIES_DIR,
    _extract_jsonld, _probe_dom_selectors,
)

FAILED_SITES = [
    {"name": "Jiji", "url": "https://jiji.ng/ojo/tv-dvd-equipment/2025-hisense-85-inch-qled-smart-flameless-tv-netflix-wifi-uPtTJ444KENWyVelkBMayEsN.html"},
    {"name": "Slot", "url": "https://slot.ng/index.php/samsung-galaxy-s25-128gb.html"},
    {"name": "Kusnap", "url": "https://kusnap.com/product/7061005-starlink-internet"},
    {"name": "Obiwezy", "url": "https://obiwezy.com/apple-iphone-16-pro-single-sim-256gb-natural-titanium-brand-new-4549995532814.html"},
    {"name": "Printivo", "url": "https://printivo.com/product/one-sided-business-cards"},
    {"name": "Selar", "url": "https://selar.com/DMCourse"},
    {"name": "PayPorte", "url": "https://payporte.com/products/crochet-mini-dress"},
    {"name": "Wakanow", "url": "https://www.wakanow.com/en-ng/hotel"},
    {"name": "Bitmarte", "url": "https://bitmarte.com/"},
]


async def http_extract(name, url):
    """Direct HTTP extraction: JSON-LD first, then HTML parsing fallback."""
    domain = get_domain(url)
    
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as c:
            resp = await c.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            })
            resp.raise_for_status()
            html = resp.text
    except Exception as e:
        return None, f"HTTP error: {type(e).__name__}"

    # Try JSON-LD
    ld_pattern = r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>'
    for m in re.finditer(ld_pattern, html, re.DOTALL):
        try:
            data = json.loads(m.group(1))
            if isinstance(data, dict):
                if data.get("@type") == "Product":
                    return {"title": data.get("name",""), "price": _extract_price(data)}, None
                graph = data.get("@graph", [])
                for item in graph:
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        return {"title": item.get("name",""), "price": _extract_price(item)}, None
        except json.JSONDecodeError:
            continue

    return None, "no jsonld"


def _extract_price(data):
    offers = data.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if isinstance(offers, dict):
        price = offers.get("price")
        if price is not None:
            try:
                return float(str(price).replace(",",""))
            except ValueError:
                pass
        spec = offers.get("priceSpecification", {})
        if isinstance(spec, list):
            spec = spec[0] if spec else {}
        if isinstance(spec, dict):
            price = spec.get("price")
            if price is not None:
                try:
                    return float(str(price).replace(",",""))
                except ValueError:
                    pass
    return 0


async def save_dom_strategy(domain, url, page):
    """Try DOM probe and save strategy if successful."""
    strategy = await _probe_dom_selectors(domain, url)
    if strategy:
        save_strategy(strategy)
        return strategy
    return None


def extract_from_html(html, url):
    """Fallback: extract title from <title> and <meta> tags, price from text."""
    title = ""
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL)
    if m:
        title = m.group(1).strip()
    
    m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
    if not m:
        m = re.search(r'<meta[^>]+name="twitter:title"[^>]+content="([^"]+)"', html)
    if m:
        title = m.group(1)
    
    price = None
    m = re.search(r'<meta[^>]+property="product:price:amount"[^>]+content="([^"]+)"', html)
    if m:
        try:
            price = float(m.group(1))
        except ValueError:
            pass
    
    if price is None:
        patterns = [
            r'₦\s*([0-9,]+(?:\.[0-9]+)?)',
            r'NGN\s*([0-9,]+(?:\.[0-9]+)?)',
            r'price["\']?\s*[:=]\s*["\']?([0-9,.]+)',
        ]
        text_around = html[html.lower().find("price"):html.lower().find("price")+200] if "price" in html.lower() else html[:2000]
        for p in patterns:
            m = re.search(p, text_around)
            if m:
                try:
                    price = float(m.group(1).replace(",",""))
                    break
                except ValueError:
                    pass
    
    if title and price:
        return {"title": title[:200], "price": price, "currency": "NGN"}
    return None


async def scrape_with_retry(site, max_retries=5):
    name = site["name"]
    url = site["url"]
    domain = get_domain(url)

    for attempt in range(1, max_retries + 1):
        delay = min(2 ** attempt, 30)
        
        # Phase 1: Try JSON-LD via HTTP (fast path)
        data, err = await http_extract(name, url)
        if data and data.get("title") and data.get("price", 0) > 0:
            return {"name": name, "domain": domain, "success": True, "method": "jsonld", "data": data}

        # Phase 1b: Try full scrape_url which includes JSON-LD + browser fallback
        try:
            data = await scrape_url(url)
        except Exception as e:
            data = None
        
        if data and data.get("title") and data.get("price", 0) > 0:
            strat = load_strategy(domain)
            method = strat.get("strategy_type", "?") if strat else "?"
            return {"name": name, "domain": domain, "success": True, "method": method, "data": data}

        if attempt >= max_retries:
            return {"name": name, "domain": domain, "success": False, "error": err or "failed"}
        
        await asyncio.sleep(delay)
    
    return {"name": name, "domain": domain, "success": False}


class Command(BaseCommand):
    help = "Retry failed sites with improved URLs and methods"

    def handle(self, *args, **options):
        print(f"Retrying {len(FAILED_SITES)} failed sites (max 5 retries each, 2 concurrent)...\n")
        results = asyncio.run(self._run_all())
        self._report(results)
        self._list_strategies()

    async def _run_all(self):
        sem = asyncio.Semaphore(2)
        async def run_one(site):
            async with sem:
                return await scrape_with_retry(site, 5)
        tasks = [run_one(s) for s in FAILED_SITES]
        results = await asyncio.gather(*tasks)
        return results

    def _report(self, results):
        print("\n\n" + "=" * 70)
        print("  FINAL RESULTS")
        print("=" * 70)
        for r in results:
            if r["success"]:
                d = r["data"]
                print(f"  ✅ {r['name']:16s} ({r['domain']:25s}) method={r['method']:6s} {d.get('title','')[:45]} | {d.get('currency','NGN')} {d['price']:.2f}")
            else:
                print(f"  ❌ {r['name']:16s} ({r['domain']:25s}) {r.get('error','')}")

    def _list_strategies(self):
        print(f"\n  {'─'*66}")
        print("  Strategy files:")
        for sf in sorted(STRATEGIES_DIR.glob("*.json")):
            s = json.loads(sf.read_text())
            icon = "✅" if s.get("success_count", 0) > 0 else "❌"
            print(f"    {icon} {sf.name} (s={s.get('success_count',0)} f={s.get('failure_count',0)})")
