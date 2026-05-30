#!/usr/bin/env python3
"""
Scrape all tracked products using CloakBrowser.

For each product URL:
  1. Check for cached strategy (data/strategies/<domain>.json)
  2. If valid -> extract directly via API or DOM eval
  3. If missing/failed -> forge new strategy (API-first, DOM fallback)
  4. Save data via the API server
"""
import asyncio

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


async def scrape_product(url: str) -> dict | None:
    domain = get_domain(url)
    strategy = load_strategy(domain)

    if strategy and not needs_rediscovery(strategy):
        print(f"  Using cached {strategy.get('strategy_type', '?')} strategy for {domain}")
        data = await extract_with_strategy(url, strategy)
        if data and data.get("title") and data.get("price", 0) > 0:
            mark_success(strategy)
            return data
        mark_failure(strategy)
        print(f"  Cached strategy failed for {domain}, re-forging...")

    print(f"  Forging strategy for {domain} (API first, DOM fallback)...")
    strategy = await forge_strategy(url)
    if not strategy:
        print(f"  Could not forge strategy for {domain}")
        return None

    mark_success(strategy)
    print(f"  Strategy saved for {domain} ({strategy.get('strategy_type', '?')})")

    data = await extract_with_strategy(url, strategy)
    return data


async def main():
    async with httpx.AsyncClient(base_url=API_BASE) as client:
        resp = await client.get("/api/products")
        products = resp.json()

    if not products:
        print("No products to scrape.")
        return

    for product in products:
        url = product["url"]
        print(f"\nScraping {url}...")
        data = await scrape_product(url)
        if data is None:
            print(f"  Failed to scrape {url}")
            continue

        currency = data.get("currency", "NGN")
        symbol = "₦" if currency == "NGN" else "$"
        print(f"  Title: {data['title'][:60]}...")
        print(f"  Price: {symbol}{data['price']:.2f}")

        async with httpx.AsyncClient(base_url=API_BASE) as client:
            await client.patch(f"/api/products/{product['id']}", json={
                "title": data["title"],
                "image_url": data["image_url"],
                "rating": data["rating"],
            })
            await client.post(
                f"/api/snapshots?product_id={product['id']}&price={data['price']}&currency={currency}"
            )

    await _close_browser()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
