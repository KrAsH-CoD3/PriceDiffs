import asyncio
from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand
from app.strategy import scrape_url
from app.models import Product, PriceSnapshot


class Command(BaseCommand):
    help = "Scrape all tracked products once"

    def handle(self, *args, **options):
        asyncio.run(self._scrape_all())

    async def _scrape_all(self):
        products = await sync_to_async(list)(Product.objects.all())

        if not products:
            self.stdout.write("No products to scrape.")
            return

        for product in products:
            url = product.url
            self.stdout.write(f"\nScraping {url}...")
            data = await scrape_url(url)

            if not data or not data.get("title") or not data.get("price", 0) > 0:
                self.stdout.write(f"  Failed to scrape {url}")
                continue

            product.title = data.get("title", product.title)
            product.image_url = data.get("image_url", product.image_url)
            product.rating = data.get("rating", product.rating)
            await sync_to_async(product.save)()

            await sync_to_async(PriceSnapshot.objects.create)(
                product=product,
                price=data.get("price", 0),
                currency=data.get("currency", "NGN"),
            )

            symbol = "₦" if data.get("currency") == "NGN" else "$"
            self.stdout.write(f"  Title: {data.get('title', '')[:60]}...")
            self.stdout.write(f"  Price: {symbol}{data.get('price', 0):.2f}")

        self.stdout.write("\nDone.")
