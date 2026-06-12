import asyncio
import httpx
from celery import shared_task
from app.strategy import scrape_url, html_is_unavailable
from app.models import Product, PriceSnapshot, ScrapeEvent


_task_loop = None


def _run_async(coro):
    global _task_loop
    if _task_loop is None or _task_loop.is_closed():
        _task_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_task_loop)
    return _task_loop.run_until_complete(coro)


def _emit(product_id: int, event_type: str, data: dict = None):
    ScrapeEvent.objects.create(
        product_id=product_id,
        event_type=event_type,
        data=data or {},
    )


def _fetch_and_check_gone(url: str) -> bool:
    async def _check():
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
                resp = await client.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Accept": "text/html",
                    },
                )
                if resp.status_code == 404:
                    return True
                return html_is_unavailable(resp.text)
        except Exception:
            return False
    return _run_async(_check())


def _last_snapshot_data(product):
    snap = PriceSnapshot.objects.filter(product=product).order_by("-scraped_at").first()
    if snap is None:
        return None
    count = PriceSnapshot.objects.filter(product=product).count()
    return {
        "title": product.title,
        "price": snap.price,
        "currency": snap.currency,
        "image_url": product.image_url,
        "rating": product.rating,
        "scraped_at": snap.scraped_at.isoformat() if snap.scraped_at else "",
        "snapshot_count": count,
    }


@shared_task(max_retries=2, default_retry_delay=60)
def scrape_product(product_id: int):
    try:
        product = Product.objects.get(pk=product_id)
    except Product.DoesNotExist:
        return {"error": "Product not found"}

    _emit(product_id, "scraping", {"title": product.title or product.url[:80]})

    data = _run_async(scrape_url(product.url))
    if data and data.get("title") and data.get("price", 0) > 0:
        product.title = data.get("title", product.title)
        product.image_url = data.get("image_url", product.image_url)
        product.rating = data.get("rating", product.rating)
        product.save(update_fields=["title", "image_url", "rating"])
        snap = PriceSnapshot.objects.create(
            product=product,
            price=data.get("price", 0),
            currency=data.get("currency", "NGN"),
        )
        snap_count = PriceSnapshot.objects.filter(product=product).count()
        _emit(product_id, "completed", {
            "title": product.title,
            "price": snap.price,
            "currency": snap.currency,
            "image_url": product.image_url,
            "rating": product.rating,
            "scraped_at": snap.scraped_at.isoformat(),
            "snapshot_count": snap_count,
        })
        return {"product_id": product_id, "status": "scraped", "title": data["title"]}

    # Check if the product listing is gone (sold out / removed)
    if _fetch_and_check_gone(product.url):
        last = _last_snapshot_data(product)
        _emit(product_id, "unavailable", last or {"title": product.title or product.url[:80]})
        return {"product_id": product_id, "status": "unavailable"}

    _emit(product_id, "failed", {"title": product.title or product.url[:80]})
    return {"product_id": product_id, "status": "failed"}
