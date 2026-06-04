import asyncio
from celery import shared_task
from app.strategy import scrape_url, _close_browser
from app.models import Product, PriceSnapshot


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@shared_task(max_retries=2, default_retry_delay=60)
def scrape_product(product_id: int):
    try:
        product = Product.objects.get(pk=product_id)
    except Product.DoesNotExist:
        return {"error": "Product not found"}

    data = _run_async(scrape_url(product.url))
    if data and data.get("title") and data.get("price", 0) > 0:
        product.title = data.get("title", product.title)
        product.image_url = data.get("image_url", product.image_url)
        product.rating = data.get("rating", product.rating)
        product.save(update_fields=["title", "image_url", "rating"])
        PriceSnapshot.objects.create(
            product=product,
            price=data.get("price", 0),
            currency=data.get("currency", "NGN"),
        )
        return {"product_id": product_id, "status": "scraped", "title": data["title"]}

    _run_async(_close_browser())
    return {"product_id": product_id, "status": "failed"}
