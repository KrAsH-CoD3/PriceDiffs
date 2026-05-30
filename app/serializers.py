from datetime import datetime, timezone, timedelta
from app.models import Product, PriceSnapshot


def product_to_dict(product: Product) -> dict:
    return {
        "id": product.id,
        "url": product.url,
        "title": product.title,
        "image_url": product.image_url,
        "rating": product.rating,
        "created_at": product.created_at.isoformat(),
    }


def snapshot_to_dict(snapshot: PriceSnapshot) -> dict:
    return {
        "id": snapshot.id,
        "product_id": snapshot.product_id,
        "price": snapshot.price,
        "currency": snapshot.currency,
        "scraped_at": snapshot.scraped_at.isoformat(),
    }


def product_detail_to_dict(product: Product) -> dict:
    snapshots = list(product.snapshots.all())
    result = {
        "product": product_to_dict(product),
        "snapshots": [snapshot_to_dict(s) for s in snapshots],
        "current_price": None,
        "lowest_price": None,
        "highest_price": None,
        "price_change_24h": None,
    }

    if snapshots:
        result["current_price"] = snapshots[0].price
        result["lowest_price"] = min(s.price for s in snapshots)
        result["highest_price"] = max(s.price for s in snapshots)

        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent = [
            s for s in snapshots
            if s.scraped_at.replace(tzinfo=timezone.utc) >= cutoff
        ]
        if len(recent) >= 2:
            result["price_change_24h"] = round(recent[-1].price - recent[0].price, 2)

    return result
