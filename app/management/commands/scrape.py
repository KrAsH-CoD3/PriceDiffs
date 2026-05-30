import asyncio
from django.core.management.base import BaseCommand
from django.db import transaction
from app.strategy import (
    get_domain,
    load_strategy,
    needs_rediscovery,
    forge_strategy,
    extract_with_strategy,
    mark_success,
    mark_failure,
    _close_browser,
)
from app.models import Product, PriceSnapshot


class Command(BaseCommand):
    help = "Scrape all tracked products once"

    def handle(self, *args, **options):
        asyncio.run(self._scrape_all())

    async def _scrape_all(self):
        products = list(Product.objects.all())

        if not products:
            self.stdout.write("No products to scrape.")
            return

        for product in products:
            url = product.url
            domain = get_domain(url)
            self.stdout.write(f"\nScraping {url}...")

            strategy = load_strategy(domain)
            if strategy and not needs_rediscovery(strategy):
                self.stdout.write(
                    f"  Using cached {strategy.get('strategy_type', '?')} strategy for {domain}"
                )
                data = await extract_with_strategy(url, strategy)
                if data and data.get("title") and data.get("price", 0) > 0:
                    mark_success(strategy)
                else:
                    mark_failure(strategy)
                    self.stdout.write(f"  Cached strategy failed for {domain}, re-forging...")
                    strategy = await forge_strategy(url)
                    if not strategy:
                        self.stdout.write(f"  Could not forge strategy for {domain}")
                        continue
                    mark_success(strategy)
                    data = await extract_with_strategy(url, strategy)
                    if not data:
                        continue
            else:
                self.stdout.write(f"  Forging strategy for {domain}...")
                strategy = await forge_strategy(url)
                if not strategy:
                    self.stdout.write(f"  Could not forge strategy for {domain}")
                    continue
                mark_success(strategy)
                data = await extract_with_strategy(url, strategy)
                if not data:
                    continue

            product.title = data.get("title", product.title)
            product.image_url = data.get("image_url", product.image_url)
            product.rating = data.get("rating", product.rating)
            product.save()

            PriceSnapshot.objects.create(
                product=product,
                price=data.get("price", 0),
                currency=data.get("currency", "NGN"),
            )

            symbol = "₦" if data.get("currency") == "NGN" else "$"
            self.stdout.write(f"  Title: {data.get('title', '')[:60]}...")
            self.stdout.write(f"  Price: {symbol}{data.get('price', 0):.2f}")

        await _close_browser()
        self.stdout.write("\nDone.")
