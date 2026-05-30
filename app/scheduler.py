import asyncio
import os

import httpx

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

API_BASE = "http://127.0.0.1:8000"
SCRAPE_INTERVAL_SECONDS = int(os.environ.get("PRICEDIFF_SCRAPE_INTERVAL", "3600"))
_loop_task: asyncio.Task | None = None
_stop_event = asyncio.Event()


async def scrape_all():
    async with httpx.AsyncClient(base_url=API_BASE) as client:
        resp = await client.get("/api/products")
        products = resp.json()

    if not products:
        return

    for product in products:
        url = product["url"]
        domain = get_domain(url)
        strategy = load_strategy(domain)

        if strategy and not needs_rediscovery(strategy):
            data = await extract_with_strategy(url, strategy)
            if data and data.get("title") and data.get("price", 0) > 0:
                mark_success(strategy)
            else:
                mark_failure(strategy)
                continue
        else:
            strategy = await forge_strategy(url)
            if not strategy:
                continue
            mark_success(strategy)
            data = await extract_with_strategy(url, strategy)
            if not data:
                continue

        currency = data.get("currency", "NGN")
        async with httpx.AsyncClient(base_url=API_BASE) as client:
            await client.patch(f"/api/products/{product['id']}", json={
                "title": data["title"],
                "image_url": data["image_url"],
                "rating": data["rating"],
            })
            await client.post(
                f"/api/snapshots?product_id={product['id']}&price={data['price']}&currency={currency}"
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
