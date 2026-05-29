#!/usr/bin/env python3
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import httpx

API_BASE = "http://127.0.0.1:8000"
BROWSER_ACT = "browser-act"  # CLI tool installed by the BrowserAct skill


async def scrape_product(url: str) -> dict | None:
    """Use BrowserAct CLI to extract product data from a URL."""
    cmds = [
        [BROWSER_ACT, "browser", "create", "--stealth"],
        [BROWSER_ACT, "browser", "open", url],
        [BROWSER_ACT, "browser", "wait", "stable"],
        [BROWSER_ACT, "browser", "get", "markdown"],
        [BROWSER_ACT, "browser", "close"],
    ]
    all_output = []
    proc = None
    try:
        for cmd in cmds:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            out = stdout.decode().strip()
            if out:
                all_output.append(out)
    except FileNotFoundError:
        print("BrowserAct CLI not found. Install: npx skills @browseract/skills --skill-browser-act")
        return None
    except Exception as e:
        print(f"BrowserAct error: {e}")
        return None

    markdown = "\n".join(all_output)
    return extract_product_data(markdown, url)


def extract_product_data(markdown: str, url: str) -> dict | None:
    """Parse markdown output for title, price, rating, image."""
    lines = markdown.splitlines()
    title = ""
    price = 0.0
    rating = ""
    image_url = ""

    for line in lines:
        lower = line.lower()
        if not title and line.strip() and not line.startswith(("#", "!", "[", "http")):
            if len(line) > 10 and len(line) < 200:
                title = line.strip()
        if "price" in lower or "$" in line:
            import re
            match = re.search(r'\$?(\d+[.,]\d{2})', line)
            if match:
                price = float(match.group(1).replace(",", ""))
        if "rating" in lower:
            match = re.search(r'[\d.]+(?=\s*out of\s*5)', line)
            if match:
                rating = match.group(0)

    return {"title": title, "price": price, "rating": rating, "image_url": image_url}


async def main():
    async with httpx.AsyncClient(base_url=API_BASE) as client:
        resp = await client.get("/api/products")
        products = resp.json()

    if not products:
        print("No products to scrape.")
        return

    for product in products:
        print(f"Scraping {product['url']}...")
        data = await scrape_product(product["url"])
        if data is None:
            print(f"  Failed to scrape {product['url']}")
            continue

        print(f"  Title: {data['title'][:60]}...")
        print(f"  Price: ${data['price']:.2f}")

        async with httpx.AsyncClient(base_url=API_BASE) as client:
            await client.patch(f"/api/products/{product['id']}", json={
                "title": data["title"],
                "image_url": data["image_url"],
                "rating": data["rating"],
            })
            await client.post(f"/api/snapshots?product_id={product['id']}&price={data['price']}")

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
