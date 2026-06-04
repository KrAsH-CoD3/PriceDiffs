import asyncio
import time
import json
from pathlib import Path
from urllib.parse import urlparse
from django.core.management.base import BaseCommand
from app.strategy import scrape_url, _close_browser, forge_strategy, load_strategy, get_domain, STRATEGIES_DIR

NEW_SITES = [
    {
        "name": "Jiji",
        "url": "https://jiji.ng/ikeja/headphones/soundcore-life-p2i-true-wireless-earbuds-utMhS4lawfnI31I8ifnP67Q5.html",
    },
    {
        "name": "Kara",
        "url": "https://kara.com.ng/iphone-17-promax-256gb",
    },
    {
        "name": "Slot",
        "url": "https://slot.ng/index.php/samsung-galaxy-a16-4gb-128gb.html",
    },
    {
        "name": "Kusnap",
        "url": "https://kusnap.com/product/2399173-macbook-pro-2023-m3-14-inch",
    },
    {
        "name": "Sellatease",
        "url": "https://sellatease.com/for-sale/health-beauty/monoi-oil-natural-hair-spray-lotion-100ml_i78238",
    },
    {
        "name": "Obiwezy",
        "url": "https://obiwezy.com/apple-iphone-16-pro-single-sim-256gb-natural-titanium-brand-new-4549995532814.html",
    },
    {
        "name": "Printivo",
        "url": "https://printivo.com/product/label-stickers",
    },
    {
        "name": "AssetPharmacy",
        "url": "https://assetpharmacy.com/product/panadol-pain-fever-per-sachet/",
    },
    {
        "name": "Comilmart",
        "url": "https://comilmart.com/product/mgo-970-umf-22-manuka-honey-250g-premium-new-zealand-certified/",
    },
    {
        "name": "Ajebomarket",
        "url": "https://ajebomarket.com/products/mens-sneakers-athletic-shoes-nb-fuel-cell-rebel-v4",
    },
    {
        "name": "NexusSupermarket",
        "url": "https://www.nexussupermarket.com/product/method-men-sea-surf-body-wash-532-ml/",
    },
    {
        "name": "Selar",
        "url": "https://selar.com/mastering-digital-products?currency=USD",
    },
    {
        "name": "PayPorte",
        "url": "https://payporte.com/",
    },
    {
        "name": "Wakanow",
        "url": "https://www.wakanow.com/en-ng/",
    },
    {
        "name": "Bitmarte",
        "url": "https://bitmarte.com/",
    },
]


class Command(BaseCommand):
    help = "Discover and test scraping on new Nigerian e-commerce sites with retries"

    def add_arguments(self, parser):
        parser.add_argument("--retries", type=int, default=7, help="Max retries")
        parser.add_argument("--concurrent", type=int, default=3, help="Concurrent scrapes")

    def handle(self, *args, **options):
        max_retries = options["retries"]
        concurrency = options["concurrent"]
        results = asyncio.run(self._run_all(max_retries, concurrency))
        self._print_report(results)

    async def _scrape_with_retry(self, site, max_retries):
        name = site["name"]
        url = site["url"]
        domain = get_domain(url)

        self.stdout.write(f"\n{'='*60}")
        self.stdout.write(f"  {name} ({domain})")
        self.stdout.write(f"  URL: {url}")
        self.stdout.write(f"{'='*60}")

        for attempt in range(1, max_retries + 1):
            self.stdout.write(f"  Attempt {attempt}/{max_retries}...")

            try:
                data = await scrape_url(url)
            except Exception as e:
                self.stdout.write(f"  Exception: {e}")
                data = None

            if data and data.get("title") and data.get("price", 0) > 0:
                self.stdout.write(f"  SUCCESS! Title: {data['title'][:60]}")
                self.stdout.write(f"  Price: {data.get('currency','NGN')} {data['price']:.2f}")
                strategy = load_strategy(domain)
                return {
                    "name": name,
                    "domain": domain,
                    "url": url,
                    "success": True,
                    "attempts": attempt,
                    "data": data,
                    "strategy_type": strategy.get("strategy_type", "unknown") if strategy else "unknown",
                }

            if attempt < max_retries:
                delay = min(2 ** attempt, 60)
                self.stdout.write(f"  Failed, retrying in {delay}s...")
                await asyncio.sleep(delay)

        return {
            "name": name,
            "domain": domain,
            "url": url,
            "success": False,
            "attempts": max_retries,
            "data": None,
        }

    async def _run_all(self, max_retries, concurrency):
        sem = asyncio.Semaphore(concurrency)

        async def bound_scrape(site):
            async with sem:
                return await self._scrape_with_retry(site, max_retries)

        tasks = [bound_scrape(site) for site in NEW_SITES]
        results = await asyncio.gather(*tasks)
        await _close_browser()
        return results

    def _print_report(self, results):
        self.stdout.write("\n\n")
        self.stdout.write("=" * 70)
        self.stdout.write("  DISCOVERY REPORT")
        self.stdout.write("=" * 70)

        successful = [r for r in results if r["success"]]
        failed = [r for r in results if not r["success"]]

        self.stdout.write(f"\nTotal sites tested: {len(results)}")
        self.stdout.write(f"Successful: {len(successful)}")
        self.stdout.write(f"Failed after {results[0]['attempts'] if results else 0} retries: {len(failed)}")

        if successful:
            self.stdout.write(f"\n{'─'*70}")
            self.stdout.write("  SUCCESSFUL SITES (new strategies cached)")
            self.stdout.write(f"{'─'*70}")
            for r in successful:
                data = r["data"]
                self.stdout.write(f"  ✅ {r['name']:20s} ({r['domain']:25s}) "
                                  f"type={r['strategy_type']:5s} "
                                  f"title={data['title'][:40]}")

        if failed:
            self.stdout.write(f"\n{'─'*70}")
            self.stdout.write("  FAILED SITES (all retries exhausted)")
            self.stdout.write(f"{'─'*70}")
            for r in failed:
                self.stdout.write(f"  ❌ {r['name']:20s} ({r['domain']:25s})")

        self.stdout.write(f"\n{'─'*70}")
        self.stdout.write("  STRATEGY FILES CACHED")
        self.stdout.write(f"{'─'*70}")
        strategy_files = sorted(STRATEGIES_DIR.glob("*.json"))
        for sf in strategy_files:
            s = json.loads(sf.read_text())
            status = "✅" if s.get("success_count", 0) > 0 else "❌"
            self.stdout.write(f"  {status} {sf.name} (success={s.get('success_count',0)}, failures={s.get('failure_count',0)})")
