import asyncio
import json
from pathlib import Path
from django.core.management.base import BaseCommand
from app.strategy import scrape_url, get_domain, STRATEGIES_DIR
import time

NEW_SITES = [
    {"name": "Jiji", "url": "https://jiji.ng/ikeja/headphones/soundcore-life-p2i-true-wireless-earbuds-utMhS4lawfnI31I8ifnP67Q5.html"},
    {"name": "Kara", "url": "https://kara.com.ng/iphone-17-promax-256gb"},
    {"name": "Slot", "url": "https://slot.ng/index.php/samsung-galaxy-a16-4gb-128gb.html"},
    {"name": "Kusnap", "url": "https://kusnap.com/product/2399173-macbook-pro-2023-m3-14-inch"},
    {"name": "Sellatease", "url": "https://sellatease.com/for-sale/health-beauty/monoi-oil-natural-hair-spray-lotion-100ml_i78238"},
    {"name": "Obiwezy", "url": "https://obiwezy.com/apple-iphone-16-pro-single-sim-256gb-natural-titanium-brand-new-4549995532814.html"},
    {"name": "Printivo", "url": "https://printivo.com/product/label-stickers"},
    {"name": "AssetPharmacy", "url": "https://assetpharmacy.com/product/panadol-pain-fever-per-sachet/"},
    {"name": "Comilmart", "url": "https://comilmart.com/product/mgo-970-umf-22-manuka-honey-250g-premium-new-zealand-certified/"},
    {"name": "Ajebomarket", "url": "https://ajebomarket.com/products/mens-sneakers-athletic-shoes-nb-fuel-cell-rebel-v4"},
    {"name": "NexusSupermarket", "url": "https://www.nexussupermarket.com/product/method-men-sea-surf-body-wash-532-ml/"},
    {"name": "Selar", "url": "https://selar.com/mastering-digital-products?currency=USD"},
    {"name": "PayPorte", "url": "https://payporte.com/"},
    {"name": "Wakanow", "url": "https://www.wakanow.com/en-ng/"},
    {"name": "Bitmarte", "url": "https://bitmarte.com/"},
]


class Command(BaseCommand):
    help = "Fast discover test for new Nigerian e-commerce sites with retries"

    def handle(self, *args, **options):
        results = asyncio.run(self._run_all())
        self._print_report(results)

    async def _scrape_once(self, site, attempt=1, max_retries=7):
        name = site["name"]
        url = site["url"]
        domain = get_domain(url)

        delay = min(2 ** attempt, 60)
        self.stdout.write(f"  [{name:16s}] Attempt {attempt}/{max_retries}...", ending="")
        self.stdout.flush()

        try:
            data = await scrape_url(url)
        except Exception as e:
            data = None
            self.stdout.write(f" exception: {type(e).__name__}")

        if data and data.get("title") and data.get("price", 0) > 0:
            self.stdout.write(f" ✅ {data['title'][:50]} | {data.get('currency','NGN')} {data['price']:.2f}".replace("&amp;", "&"))
            return {"name": name, "domain": domain, "success": True, "attempts": attempt}

        if attempt < max_retries:
            self.stdout.write(f" ❌ (will retry in {delay}s)")
            await asyncio.sleep(delay)
            return await self._scrape_once(site, attempt + 1, max_retries)
        else:
            self.stdout.write(f" ❌ (all retries exhausted)")
            return {"name": name, "domain": domain, "success": False, "attempts": max_retries}

    async def _run_all(self):
        results = []
        for site in NEW_SITES:
            self.stdout.write(f"\n{'='*60}")
            self.stdout.write(f"  {site['name']} ({get_domain(site['url'])})")
            self.stdout.write(f"  URL: {site['url']}")
            self.stdout.write(f"{'='*60}")
            result = await self._scrape_once(site, 1, 7)
            results.append(result)
        return results

    def _print_report(self, results):
        self.stdout.write("\n\n" + "=" * 70)
        self.stdout.write("  FINAL DISCOVERY REPORT")
        self.stdout.write("=" * 70)

        successful = [r for r in results if r["success"]]
        failed = [r for r in results if not r["success"]]

        self.stdout.write(f"\n  Total tested: {len(results)}")
        self.stdout.write(f"  Successful:   {len(successful)}")
        self.stdout.write(f"  Failed:       {len(failed)}")

        if successful:
            self.stdout.write(f"\n  {'─'*66}")
            self.stdout.write("  ✅ SUCCESSFUL")
            for r in successful:
                self.stdout.write(f"    {r['name']:18s} ({r['domain']:25s}) attempts={r['attempts']}")

        if failed:
            self.stdout.write(f"\n  {'─'*66}")
            self.stdout.write("  ❌ FAILED (likely blocking or no product data)")
            for r in failed:
                self.stdout.write(f"    {r['name']:18s} ({r['domain']:25s}) attempts={r['attempts']}")

        self.stdout.write(f"\n  {'─'*66}")
        self.stdout.write("  New strategy files cached:")
        for sf in sorted(STRATEGIES_DIR.glob("*.json")):
            s = json.loads(sf.read_text())
            status = "✅" if s.get("success_count", 0) > 0 else "❌"
            self.stdout.write(f"    {status} {sf.name}")
