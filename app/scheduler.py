import asyncio
from datetime import datetime, timezone

import httpx
from asgiref.sync import sync_to_async
from app.strategy import scrape_url, html_is_unavailable

_loop_task: asyncio.Task | None = None
_stop_event = asyncio.Event()

# Fixed scrape hours: midnight, 6am, noon, 6pm
_SCRAPE_HOURS = {0, 6, 12, 18}


async def _check_gone(url: str) -> bool:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html",
            })
            if resp.status_code == 404:
                return True
            return html_is_unavailable(resp.text)
    except Exception:
        return False


async def scrape_all():
    from app.models import Product, PriceSnapshot, ScrapeEvent
    products = await sync_to_async(list)(Product.objects.all())
    if not products:
        return
    for product in products:
        data = await scrape_url(product.url)
        if data and data.get("title") and data.get("price", 0) > 0:
            product.title = data.get("title", product.title)
            product.image_url = data.get("image_url", product.image_url)
            product.rating = data.get("rating", product.rating)
            await sync_to_async(product.save)()
            await sync_to_async(PriceSnapshot.objects.create)(
                product=product,
                price=data.get("price", 0),
                currency=data.get("currency", "NGN"),
            )
        elif data is None and await _check_gone(product.url):
            last = await sync_to_async(
                lambda: PriceSnapshot.objects.filter(product=product).order_by("-scraped_at").first()
            )()
            count = await sync_to_async(
                lambda: PriceSnapshot.objects.filter(product=product).count()
            )()
            event_data = {
                "title": product.title or "",
                "price": last.price if last else None,
                "currency": last.currency if last else "NGN",
                "image_url": product.image_url or "",
                "rating": product.rating or "",
                "scraped_at": last.scraped_at.isoformat() if last and last.scraped_at else "",
                "snapshot_count": count,
            }
            await sync_to_async(ScrapeEvent.objects.create)(
                product_id=product.id,
                event_type="unavailable",
                data=event_data,
            )


async def _run_loop():
    while not _stop_event.is_set():
        now = datetime.now(timezone.utc)
        if now.hour in _SCRAPE_HOURS and now.minute == 0:
            try:
                await scrape_all()
            except Exception:
                pass
            await asyncio.sleep(61)
        await asyncio.sleep(30)


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
    await asyncio.sleep(0)
