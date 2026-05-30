import asyncio
import os

from app.strategy import scrape_url, _close_browser

SCRAPE_INTERVAL_SECONDS = int(os.environ.get("PRICEDIFF_SCRAPE_INTERVAL", "3600"))
_loop_task: asyncio.Task | None = None
_stop_event = asyncio.Event()


def _import_models():
    import django
    import os
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pricediff.settings")
    django.setup()
    from app.models import Product, PriceSnapshot
    return Product, PriceSnapshot


async def scrape_all():
    Product, PriceSnapshot = _import_models()

    products = list(Product.objects.all())
    if not products:
        return

    for product in products:
        url = product.url
        data = await scrape_url(url)
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


async def _run_loop():
    while not _stop_event.is_set():
        try:
            await scrape_all()
        except Exception:
            pass
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=SCRAPE_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def start():
    global _loop_task
    if _loop_task is None or _loop_task.done():
        _stop_event.clear()
        _loop_task = asyncio.create_task(_run_loop())


async def stop():
    if _loop_task and not _loop_task.done():
        _stop_event.set()
        _loop_task.cancel()
        try:
            await _loop_task
        except (asyncio.CancelledError, Exception):
            pass
    await _close_browser()
