"""Utility to fetch market/token IDs from a Polymarket event slug.

Parses the event page `https://polymarket.com/event/<slug>` and extracts the
market id plus clob token ids (order follows outcomes list).
"""

import json
import re
from datetime import datetime
from typing import Dict

import httpx


def fetch_market_from_slug(slug: str) -> Dict[str, str]:
    # Allow slugs that include query params (e.g., copied from the browser)
    slug = slug.split("?")[0]
    url = f"https://polymarket.com/event/{slug}"
    resp = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    resp.raise_for_status()

    # Extract __NEXT_DATA__ JSON payload
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.DOTALL)
    if not m:
        raise RuntimeError("__NEXT_DATA__ payload not found on page")
    payload = json.loads(m.group(1))

    queries = payload.get("props", {}).get("pageProps", {}).get("dehydratedState", {}).get("queries", [])
    market = None
    for q in queries:
        data = q.get("state", {}).get("data")
        if isinstance(data, dict) and "markets" in data:
            for mk in data["markets"]:
                if mk.get("slug") == slug:
                    market = mk
                    break
        if market:
            break

    if not market:
        raise RuntimeError("Market slug not found in dehydrated state")

    clob_tokens = market.get("clobTokenIds") or []
    outcomes = market.get("outcomes") or []
    if len(clob_tokens) != 2 or len(outcomes) != 2:
        raise RuntimeError("Expected binary market with two clob tokens")

    return {
        "market_id": market.get("id", ""),
        "yes_token_id": clob_tokens[0],
        "no_token_id": clob_tokens[1],
        "outcomes": outcomes,
        "question": market.get("question", ""),
        "start_date": market.get("startDate"),
        "end_date": market.get("endDate"),
    }


def next_slug(slug: str) -> str:
    # Increment the trailing epoch-like number by 900 seconds (15m)
    m = re.match(r"(.+-)(\d+)$", slug)
    if not m:
        raise ValueError(f"Slug not in expected format: {slug}")
    prefix, num = m.groups()
    return f"{prefix}{int(num) + 900}"


def parse_iso(dt: str) -> datetime | None:
    if not dt:
        return None
    try:
        return datetime.fromisoformat(dt.replace("Z", "+00:00"))
    except Exception:
        return None


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python -m src.lookup <slug>")
        sys.exit(1)
    info = fetch_market_from_slug(sys.argv[1])
    print(json.dumps(info, indent=2))