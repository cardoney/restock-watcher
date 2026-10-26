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


_MICRO_AVAIL_RE = re.compile(
    r"<[^>]+itemprop=[\"']availability[\"'][^>]*(?:content|href)=[\"']([^\"']+)[\"']"
    r"|<[^>]+(?:content|href)=[\"']([^\"']+)[\"'][^>]*itemprop=[\"']availability[\"']",
    re.IGNORECASE,
)
_MICRO_PRICE_RE = re.compile(
    r"<[^>]+itemprop=[\"']price[\"'][^>]*content=[\"']([^\"']+)[\"']"
    r"|<[^>]+content=[\"']([^\"']+)[\"'][^>]*itemprop=[\"']price[\"']",
    re.IGNORECASE,
)


def parse_microdata(body: str) -> tuple[bool | None, float | None, str]:
    """schema.org microdata (itemprop="availability" / itemprop="price")."""
    m = _MICRO_AVAIL_RE.search(body)
    if not m:
        return None, None, "no itemprop availability"
    avail = _availability_word((m.group(1) or m.group(2)).rsplit("/", 1)[-1])
    pm = _MICRO_PRICE_RE.search(body)
    price = _to_price((pm.group(1) or pm.group(2))) if pm else None
    if avail is None:
        return None, price, "unrecognized itemprop availability"
    return avail, price, ""


def parse_auto(body: str) -> tuple[bool | None, float | None, str]:
    """Try every page-based method in turn; use the first that finds a stock status."""
    notes = []
    for parser in (parse_meta, parse_jsonld, parse_microdata):
        avail, price, note = parser(body)
        if avail is not None:
            return avail, price, ""
        notes.append(note)
    return None, None, "; ".join(n for n in notes if n)


PARSERS = {"shopify": parse_shopify, "meta": parse_meta, "jsonld": parse_jsonld,
           "microdata": parse_microdata, "auto": parse_auto}


# ---------------------------------------------------------------- new-listing search
# A product-page check can't see a store relisting the same item under a new URL
# (a "-2" copy, a separate restock listing). Shopify's public predictive search,
# /search/suggest.json, returns each matching product's title, URL, price and
# availability, so a shopify_search item searches the store and alerts when a
# matching listing that isn't already in the watchlist is in stock.

def listing_key(url: str) -> str:
    """Store host + /products/<handle>, so one listing matches however its URL is written."""
    m = re.match(r"https?://(?:www\.)?([^/?#]+)([^?#]*)", url.strip().lower())
    if not m:
        return url.strip().lower()
    host, path = m.groups()
    i = path.find("/products/")
    return host + (path[i:] if i >= 0 else path).rstrip("/")


def parse_shopify_search(body: str, item: dict, watched=frozenset()):
    """Returns (available, price, note, url) for the cheapest in-stock matching listing not already watched.

    A listing matches when its title contains every word in item["match"] and none in item["exclude"]
    (case-insensitive).
    """
    try:
        products = json.loads(body)["resources"]["results"]["products"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None, None, "not a Shopify search response", None
    origin = re.match(r"https?://[^/]+", item["check_url"]).group(0)
    need = [w.lower() for w in item.get("match", [])]
    skip = [w.lower() for w in item.get("exclude", [])]
    found = []
    for p in products:
        title = html.unescape(str(p.get("title", "")))
        low = title.lower()
        if not need or not all(w in low for w in need) or any(w in low for w in skip):
            continue
        url = str(p.get("url", "")).split("?")[0]
        url = url if url.startswith("http") else origin + url
        if listing_key(url) in watched:
            continue
        found.append((bool(p.get("available")), _to_price(p.get("price")), title, url))
    if not found:
        return False, None, "no other matching listings", None
    in_stock = sorted((f for f in found if f[0]), key=lambda f: (f[1] is None, f[1] or 0.0))
    if in_stock:
        _, price, title, url = in_stock[0]
        return True, price, f"new listing: {title}", url
    return False, None, f"{len(found)} other matching listing(s), all sold out", None


TYPES = set(PARSERS) | {"shopify_search"}


# ---------------------------------------------------------------- logic

def classify(available: bool | None, price: float | None, max_price) -> str:
    if available is None:
        return UNKNOWN
    if not available:
        return OUT
    if max_price is not None and price is not None and price > float(max_price):
        return PRICEY
    return IN


def check_item(item: dict, fetcher=None, watched=frozenset()) -> dict:
    """watched: listing_key()s of every product page in the watchlist, used by shopify_search."""
    fetcher = fetcher or fetch
    result = {"id": item["id"], "status": UNKNOWN, "price": None, "note": "", "url": None}
    kind = item.get("type", "")
    if kind not in TYPES:
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
        if kind == "shopify_search":
            available, price, note, result["url"] = parse_shopify_search(body, item, watched)
        else:
            available, price, note = PARSERS[kind](body)
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
    buy = res.get("url") or item["product_url"]  # a new-listing search links straight to the listing it found
    found = f"Found by searching the store: {res['note']}.\n\n" if res.get("url") else ""
    body = (
        f"{marker(item['id'])}\n"
        f"{'@' + owner + ' ' if owner else ''}**{item['name']}** looks to be in stock ({price}).\n\n"
        f"**Buy here:** {buy}\n\n{found}"
        f"Detected {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. "
        f"These usually sell out within minutes. This issue closes itself when the item "
        f"goes out of stock again, and you can close it any time."
    )
    print(f"  ALERT -> {title} ({price})")
    if dry_run:
        return
    ntfy(title, f"{price} - tap to open", buy)
    if open_issue_for(item["id"]):
        return
    gh_api("POST", "/labels", {"name": "in-stock", "color": "2ea44f",
                               "description": "Restock alert from the watcher"})
    payload = {"title": title, "body": body, "labels": ["in-stock"]}
    if owner:
        payload["assignees"] = [owner]  # assignment is a second notification path
    gh_api("POST", "/issues", payload)


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


# ---------------------------------------------------------------- health
# A store that starts blocking the checker would otherwise fail silently, so an
# item that stays unreadable for HEALTH_HOURS opens one "watcher-health" issue.

HEALTH_HOURS = 2
HEALTH_MARKER = "<!-- restock-health -->"


def track_readability(entry: dict, status: str, stamp: str) -> bool:
    """Record when an item became unreadable; clear it when it reads again. Returns True if changed."""
    if status == UNKNOWN:
        if not entry.get("unreadable_since"):
            entry["unreadable_since"] = stamp
            return True
        return False
    return entry.pop("unreadable_since", None) is not None


def unreadable_items(items: list, state: dict, now: datetime) -> list:
    out = []
    for item in items:
        since = state.get(item["id"], {}).get("unreadable_since")
        if since:
            t = datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if (now - t).total_seconds() >= HEALTH_HOURS * 3600:
                out.append((item, since))
    return out


def sync_health_issue(broken: list, notes: dict):
    issues = gh_api("GET", "/issues?state=open&labels=watcher-health&per_page=20") or []
    issue = next((i for i in issues if HEALTH_MARKER in (i.get("body") or "")), None)
    if not broken:
        if issue:
            gh_api("POST", f"/issues/{issue['number']}/comments", {"body": "All store pages are readable again."})
            gh_api("PATCH", f"/issues/{issue['number']}", {"state": "closed", "state_reason": "completed"})
        return
    lines = "\n".join(f"- **{it['name']}** since {since}: {notes.get(it['id']) or 'no stock info found'} "
                      f"([page]({it['product_url']}))" for it, since in broken)
    owner = os.environ.get("NOTIFY_USER", "").strip()
    body = (f"{HEALTH_MARKER}\n{'@' + owner + ' ' if owner else ''}The watcher can't read "
            f"{len(broken)} store page(s), so it is NOT checking them:\n\n{lines}\n\n"
            "Usually the store has started blocking automated checks, or the page moved. "
            "Fix or replace the item in `watchlist.json`, or set `\"enabled\": false`. "
            "This issue updates itself and closes when every page is readable again.")
    if issue:
        if issue.get("body") != body:
            gh_api("PATCH", f"/issues/{issue['number']}", {"body": body})
        return
    gh_api("POST", "/labels", {"name": "watcher-health", "color": "d93f0b",
                               "description": "The watcher can't read a store page"})
    payload = {"title": f"Watcher can't read {len(broken)} store page(s)", "body": body,
               "labels": ["watcher-health"]}
    if owner:
        payload["assignees"] = [owner]
    gh_api("POST", "/issues", payload)


# ---------------------------------------------------------------- main

LABELS ={IN: "IN STOCK", OUT: "out of stock", PRICEY: "in stock but over max price", UNKNOWN: "couldn't check"}


def main() -> int:
    dry_run = os.environ.get("DRY_RUN") == "1"
    all_items = json.loads(WATCHLIST.read_text())["items"]
    items = [i for i in all_items if i.get("enabled", True)]
    # Listings already watched (enabled or not) are skipped by new-listing searches, so they never double-alert.
    watched = frozenset(listing_key(i["product_url"]) for i in all_items if i.get("type") != "shopify_search")
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    changed = False
    rows = []

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    notes = {}

    for item in items:
        res = check_item(item, watched=watched)
        entry = state.get(item["id"], {})
        prev = entry.get("status")
        store, should_alert, back_out = decide(prev, res["status"])
        price = f"${res['price']:.2f}" if res["price"] else ""
        print(f"[{item['id']}] {LABELS[res['status']]} {price} {res['note']}".rstrip())
        if should_alert:
            alert(item, res, dry_run)
        elif back_out:
            close_alert(item, dry_run)
        if store != prev:
            entry["status"], entry["since"] = store, stamp
            changed = True
        changed |= track_readability(entry, res["status"], stamp)
        state[item["id"]] = entry
        notes[item["id"]] = res["note"]
        rows.append(f"| {item['name']} | {LABELS[res['status']]} | {price} | {res['note']} |")

    if changed and not dry_run:
        STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    broken = unreadable_items(items, state, now)
    print(f"health: {len(broken)} store page(s) unreadable for {HEALTH_HOURS}+ hours")
    if not dry_run:
        try:
            sync_health_issue(broken, notes)
        except Exception as e:  # never let the health report break stock checks
            print(f"  health issue update failed: {e}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("### Restock check\n\n| Item | Status | Price | Note |\n|---|---|---|---|\n")
            f.write("\n".join(rows) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
