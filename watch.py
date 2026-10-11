#!/usr/bin/env python3
"""Restock watcher.

Checks each product in watchlist.json and opens a GitHub issue (which emails
and pushes to the repo owner) when an item goes from out of stock to in stock
at or below its max_price. Optionally also sends a push via ntfy.sh when the
NTFY_TOPIC secret is set.

State (last known status per item) lives in state.json and is committed back
to the repo by the workflow only when something changes, so alerts fire once
per restock instead of every five minutes.

Standard library only, so the workflow needs no install step.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WATCHLIST = ROOT / "watchlist.json"
STATE = ROOT / "state.json"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0 Safari/537.36 restock-watcher/1.0 (personal, low-frequency)"
)
TIMEOUT = 20

IN = "in_stock"
OUT = "out_of_stock"
PRICEY = "in_stock_over_max"
UNKNOWN = "unknown"


# ---------------------------------------------------------------- fetching

def fetch(url: str) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, ""


# ---------------------------------------------------------------- parsers
# Each parser returns (available: bool | None, price: float | None, note: str)

def parse_shopify(body: str) -> tuple[bool | None, float | None, str]:
    """Shopify /products/<handle>.js feed. Price is in cents."""
    data = json.loads(body)
    available = data.get("available")
    price = None
    if isinstance(data.get("price"), (int, float)):
        price = data["price"] / 100.0
    # If any variant is available, treat the product as available.
    variants = data.get("variants") or []
    if variants:
        avail_variants = [v for v in variants if v.get("available")]
        if avail_variants:
            available = True
            prices = [v.get("price") for v in avail_variants if isinstance(v.get("price"), (int, float))]
            if prices:
                price = min(prices) / 100.0
    if available is None:
        return None, price, "no 'available' field in feed"
    return bool(available), price, ""


_META_RE = re.compile(
    r"<meta[^>]+(?:property|name)=[\"']([^\"']+)[\"'][^>]*content=[\"']([^\"']*)[\"']"
    r"|<meta[^>]+content=[\"']([^\"']*)[\"'][^>]*(?:property|name)=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)


def _metas(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _META_RE.finditer(body):
        if m.group(1):
            key, val = m.group(1), m.group(2)
        else:
            key, val = m.group(4), m.group(3)
        out.setdefault(key.strip().lower(), html.unescape(val.strip()))
    return out


def _availability_word(text: str) -> bool | None:
    t = text.strip().lower().replace("https://schema.org/", "").replace("http://schema.org/", "")
    t = t.replace("_", " ").replace("-", " ")
    # Pre-orders and back-orders can be bought online, so they count as available.
    if t in {"instock", "in stock", "limitedavailability", "limited availability", "onlineonly",
             "online only", "preorder", "pre order", "presale", "backorder", "back order"}:
        return True
    if t in {"outofstock", "out of stock", "soldout", "sold out", "discontinued",
             "instoreonly", "in store only"}:
        return False
    return None


def _to_price(val) -> float | None:
    if val is None:
        return None
    try:
        p = float(str(val).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None
    return p if p > 0 else None


def parse_meta(body: str) -> tuple[bool | None, float | None, str]:
    """Open Graph product meta tags (product:availability, product:price:amount)."""
    metas = _metas(body)
    raw = metas.get("product:availability") or metas.get("og:availability")
    price = _to_price(metas.get("product:price:amount") or metas.get("og:price:amount"))
    if raw is None:
        return None, price, "no product:availability meta tag"
    avail = _availability_word(raw)
    if avail is None:
        return None, price, f"unrecognized availability '{raw}'"
    return avail, price, ""


_LDJSON_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


def _walk_offers(node, found: list):
    if isinstance(node, list):
        for n in node:
            _walk_offers(n, found)
    elif isinstance(node, dict):
        if "availability" in node:
            found.append(node)
        for key in ("offers", "@graph", "hasVariant", "itemListElement"):
            if key in node:
                _walk_offers(node[key], found)


def parse_jsonld(body: str) -> tuple[bool | None, float | None, str]:
    """schema.org Product/Offer JSON-LD blocks."""
    offers: list[dict] = []
    for block in _LDJSON_RE.findall(body):
        try:
            _walk_offers(json.loads(block.strip()), offers)
        except json.JSONDecodeError:
            continue
    if not offers:
        return None, None, "no schema.org offers on page"
    in_prices, any_known = [], False
    for o in offers:
        a = _availability_word(str(o.get("availability", "")))
        if a is None:
            continue
        any_known = True
        if a:
            in_prices.append(_to_price(o.get("price") or o.get("lowPrice")))
    if in_prices:
        known = [p for p in in_prices if p is not None]
        return True, (min(known) if known else None), ""
    if any_known:
        price = next((_to_price(o.get("price")) for o in offers if _to_price(o.get("price"))), None)
        return False, price, ""
    return None, None, "offers had no recognizable availability"


PARSERS = {"shopify": parse_shopify, "meta": parse_meta, "jsonld": parse_jsonld}


# ---------------------------------------------------------------- logic

def classify(available: bool | None, price: float | None, max_price) -> str:
    if available is None:
        return UNKNOWN
    if not available:
        return OUT
    if max_price is not None and price is not None and price > float(max_price):
        return PRICEY
    return IN


def check_item(item: dict, fetcher=None) -> dict:
    fetcher = fetcher or fetch
    result = {"id": item["id"], "status": UNKNOWN, "price": None, "note": ""}
    parser = PARSERS.get(item.get("type", ""))
    if parser is None:
        result["note"] = f"unknown type '{item.get('type')}'"
        return result
    try:
        code, body = fetcher(item["check_url"])
    except Exception as e:  # network errors, timeouts, TLS
        result["note"] = f"fetch failed: {type(e).__name__}: {e}"[:200]
        return result
    if code != 200 or not body:
        result["note"] = f"HTTP {code} (site may block automated checks or the page moved)"
        return result
    try:
        available, price, note = parser(body)
    except Exception as e:
        result["note"] = f"parse failed: {type(e).__name__}: {e}"[:200]
        return result
    result["price"] = price
    result["note"] = note
    result["status"] = classify(available, price, item.get("max_price"))
    return result


def decide(prev_status: str | None, new_status: str) -> tuple[str, bool, bool]:
    """Return (status_to_store, should_alert, back_out).

    Unknown results never overwrite the last known status, so a blocked or
    flaky fetch can't cause a false alert or wipe out a real one.
    """
    prev = prev_status or OUT
    if new_status == UNKNOWN:
        return prev, False, False
    alert = new_status == IN and prev != IN
    back_out = prev == IN and new_status != IN
    return new_status, alert, back_out


# ---------------------------------------------------------------- notify

def gh_api(method: str, path: str, payload: dict | None = None):
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        raise RuntimeError("GITHUB_TOKEN / GITHUB_REPOSITORY not set")
    url = f"https://api.github.com/repos/{repo}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "restock-watcher",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if method == "POST" and path == "/labels" and e.code == 422:
            return None  # label already exists
        raise


def marker(item_id: str) -> str:
    return f"<!-- restock-item:{item_id} -->"


def open_issue_for(item_id: str):
    issues = gh_api("GET", "/issues?state=open&labels=in-stock&per_page=100") or []
    return next((i for i in issues if marker(item_id) in (i.get("body") or "")), None)


def ntfy(title: str, message: str, click: str):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        method="POST",
        headers={"Title": title.encode("ascii", "ignore").decode(), "Priority": "high",
                 "Tags": "rotating_light", "Click": click},
    )
    try:
        urllib.request.urlopen(req, timeout=TIMEOUT).read()
    except Exception as e:
        print(f"  ntfy push failed: {e}")


def alert(item: dict, res: dict, dry_run: bool):
    price = f"${res['price']:.2f}" if res["price"] else "price not shown"
    owner = os.environ.get("NOTIFY_USER", "").strip()
    title = f"IN STOCK: {item['name']}"
    body = (
        f"{marker(item['id'])}\n"
        f"{'@' + owner + ' ' if owner else ''}**{item['name']}** looks to be in stock ({price}).\n\n"
        f"**Buy here:** {item['product_url']}\n\n"
        f"Detected {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. "
        f"These usually sell out within minutes. This issue closes itself when the item "
        f"goes out of stock again, and you can close it any time."
    )
    print(f"  ALERT -> {title} ({price})")
    if dry_run:
        return
    ntfy(title, f"{price} - tap to open", item["product_url"])
    if open_issue_for(item["id"]):
        return
    gh_api("POST", "/labels", {"name": "in-stock", "color": "2ea44f",
                               "description": "Restock alert from the watcher"})
    gh_api("POST", "/issues", {"title": title, "body": body, "labels": ["in-stock"]})


def close_alert(item: dict, dry_run: bool):
    print(f"  back out of stock -> closing alert for {item['id']}")
    if dry_run:
        return
    issue = open_issue_for(item["id"])
    if not issue:
        return
    n = issue["number"]
    gh_api("POST", f"/issues/{n}/comments",
           {"body": f"Out of stock again as of {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}."})
    gh_api("PATCH", f"/issues/{n}", {"state": "closed", "state_reason": "completed"})


# ---------------------------------------------------------------- main

LABELS = {IN: "IN STOCK", OUT: "out of stock", PRICEY: "in stock but over max price", UNKNOWN: "couldn't check"}


def main() -> int:
    dry_run = os.environ.get("DRY_RUN") == "1"
    items = [i for i in json.loads(WATCHLIST.read_text())["items"] if i.get("enabled", True)]
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    changed = False
    rows = []

    for item in items:
        res = check_item(item)
        prev = state.get(item["id"], {}).get("status")
        store, should_alert, back_out = decide(prev, res["status"])
        price = f"${res['price']:.2f}" if res["price"] else ""
        print(f"[{item['id']}] {LABELS[res['status']]} {price} {res['note']}".rstrip())
        if should_alert:
            alert(item, res, dry_run)
        elif back_out:
            close_alert(item, dry_run)
        if store != prev:
            state[item["id"]] = {"status": store,
                                 "since": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            changed = True
        rows.append(f"| {item['name']} | {LABELS[res['status']]} | {price} | {res['note']} |")

    if changed and not dry_run:
        STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("### Restock check\n\n| Item | Status | Price | Note |\n|---|---|---|---|\n")
            f.write("\n".join(rows) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
