"""Retry only failed sites from the previous run, one at a time."""
import asyncio
import json
import sys
from django.core.management.base import BaseCommand
from app.strategy import scrape_url, forge_strategy, _close_browser, get_domain, load_strategy, STRATEGIES_DIR

FAILED_SITES = [
    {"name": "Jiji", "url": "https://jiji.ng/ikeja/headphones/soundcore-life-p2i-true-wireless-earbuds-utMhS4lawfnI31I8ifnP67Q5.html"},
    {"name": "Slot", "url": "https://slot.ng/index.php/samsung-galaxy-a16-4gb-128gb.html"},
    {"name": "Kusnap", "url": "https://kusnap.com/product/2399173-macbook-pro-2023-m3-14-inch"},
    {"name": "Obiwezy", "url": "https://obiwezy.com/apple-iphone-16-pro-single-sim-256gb-natural-titanium-brand-new-4549995532814.html"},
    {"name": "Printivo", "url": "https://printivo.com/product/label-stickers"},
    {"name": "NexusSupermarket", "url": "https://www.nexussupermarket.com/product/method-men-sea-surf-body-wash-532-ml/"},
    {"name": "Selar", "url": "https://selar.com/mastering-digital-products?currency=USD"},
    {"name": "PayPorte", "url": "https://payporte.com/"},
    {"name": "Wakanow", "url": "https://www.wakanow.com/en-ng/"},
    {"name": "Bitmarte", "url": "https://bitmarte.com/"},
]


async def try_with_delay(site, attempt, max_retries):
    name, url = site["name"], site["url"]
    domain = get_domain(url)

    delay = min(2 ** attempt, 30)
    print(f"  [{name:18s}] attempt {attempt}/{max_retries}...", end=" ", flush=True)

    try:
        data = await scrape_url(url)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:80]
        print(f"❌ EXCEPTION: {err}")
        if attempt < max_retries:
            await asyncio.sleep(delay)
            return await try_with_delay(site, attempt + 1, max_retries)
        return {"name": name, "domain": domain, "success": False, "reason": err}

    if data and data.get("title") and data.get("price", 0) > 0:
        t = data["title"].replace("&amp;", "&")[:50]
        print(f"✅ {t} | {data.get('currency','NGN')} {data['price']:.2f}")
        return {"name": name, "domain": domain, "success": True, "data": data}
    else:
        print(f"❌ no data")
        if attempt < max_retries:
            await asyncio.sleep(delay)
            return await try_with_delay(site, attempt + 1, max_retries)
        return {"name": name, "domain": domain, "success": False, "reason": "no product data"}


class Command(BaseCommand):
    help = "Retry failed sites one by one"

    def handle(self, *args, **options):
        print("Retrying failed sites (one at a time, up to 4 retries)...\n")
        results = asyncio.run(self._run_all())
        self._report(results)

    async def _run_all(self):
        all_results = []
        for site in FAILED_SITES:
            print(f"\n{'='*60}")
            print(f"  {site['name']} ({get_domain(site['url'])})")
            print(f"  {site['url']}")
            print(f"{'='*60}")
            r = await try_with_delay(site, 1, 4)
            all_results.append(r)
        await _close_browser()
        return all_results

    def _report(self, results):
        print("\n\n" + "=" * 70)
        print("  RETRY REPORT")
        print("=" * 70)
        for r in results:
            icon = "✅" if r["success"] else "❌"
            extra = f"- {r.get('reason','')}" if not r["success"] else ""
            print(f"  {icon} {r['name']:18s} ({r['domain']:25s}) {extra}")
