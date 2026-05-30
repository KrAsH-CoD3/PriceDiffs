"""
Forge methodology — JSON-LD first, network API second, DOM fallback.
Powered by CloakBrowser (stealth Chromium).

Phase 1 — Direct HTTP probe for embedded JSON-LD (no browser)
Phase 2 — Open browser, capture network traffic for API discovery
Phase 3 — DOM CSS probe fallback
Phase 4 — Strategy verification and persistence
"""
import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright

CLOAK_BINARY = None
for p in Path.home().joinpath(".cloakbrowser").glob("chromium-*/chrome"):
    CLOAK_BINARY = str(p)
if not CLOAK_BINARY:
    CLOAK_BINARY = ""

STRATEGIES_DIR = Path(__file__).resolve().parent.parent / "data" / "strategies"
MAX_FAILURES_BEFORE_REDISCOVERY = 2

# Proxy for Cloudflare-protected sites (set via env or before calling forge)
PROXY_URL = os.environ.get("PRICEDIFF_PROXY", "")


# ── Helpers ────────────────────────────────────────────────────────────

def get_domain(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")


def _strategy_path(domain: str) -> Path:
    return STRATEGIES_DIR / f"{domain}.json"


def load_strategy(domain: str) -> dict | None:
    path = _strategy_path(domain)
    if path.exists():
        return json.loads(path.read_text())
    return None


def save_strategy(strategy: dict):
    STRATEGIES_DIR.mkdir(parents=True, exist_ok=True)
    _strategy_path(strategy["domain"]).write_text(json.dumps(strategy, indent=2))


def needs_rediscovery(strategy: dict) -> bool:
    return strategy.get("failure_count", 0) > MAX_FAILURES_BEFORE_REDISCOVERY


def _save_meta(strategy: dict):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    if not strategy.get("created_at"):
        strategy["created_at"] = now
    strategy["updated_at"] = now
    save_strategy(strategy)


def mark_success(strategy: dict):
    strategy["success_count"] = strategy.get("success_count", 0) + 1
    strategy["failure_count"] = 0
    _save_meta(strategy)


def mark_failure(strategy: dict):
    strategy["failure_count"] = strategy.get("failure_count", 0) + 1
    _save_meta(strategy)


# ─── CloakBrowser browser — single instance across sessions ─────────────

_browser_instance = None
_playwright_instance = None


async def _get_browser():
    global _browser_instance, _playwright_instance
    if _browser_instance and _browser_instance.is_connected():
        return _browser_instance
    _playwright_instance = await async_playwright().start()
    launch_kwargs = {
        "executable_path": CLOAK_BINARY,
        "headless": True,
        "args": ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
    }
    if PROXY_URL:
        from urllib.parse import urlparse
        parsed = urlparse(PROXY_URL)
        proxy_config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
        if parsed.username:
            proxy_config["username"] = parsed.username
        if parsed.password:
            proxy_config["password"] = parsed.password
        launch_kwargs["proxy"] = proxy_config
    _browser_instance = await _playwright_instance.chromium.launch(**launch_kwargs)
    return _browser_instance


async def _close_browser():
    global _browser_instance, _playwright_instance
    if _browser_instance:
        try:
            await _browser_instance.close()
        except Exception:
            pass
        _browser_instance = None
    if _playwright_instance:
        try:
            await _playwright_instance.stop()
        except Exception:
            pass
        _playwright_instance = None
