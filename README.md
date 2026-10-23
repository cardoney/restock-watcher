# Restock watcher

Checks a short list of product pages every 5 minutes and alerts you the moment one comes back in stock at a sane price.

Currently watching:

| Item | Alert at or under | Stores |
|---|---|---|
| Banana Ball 2026 Collector's Box | $200 | Savannah Bananas official shop, Steel City Collectibles |
| 2026 Topps Finest Baseball Mega Box | $100 | Giant Sports Cards (2 listings), Steel City Collectibles, Midwest Cards, Dave & Adam's |

Topps.com, Fanatics, Target, Walmart and Blowout Cards sell the Topps Finest Mega Box at its $69.99 list price but block automated checks, so the twice-daily Claude search covers those.

Changing `watchlist.json` (by hand or by the daily search) triggers a check right away.

## How you get alerted

When an item goes from out of stock to in stock at or under its `max_price`, or an in-stock item's price drops to or under `max_price`, the watcher opens a GitHub issue titled **IN STOCK: …** that @mentions you and is assigned to you. GitHub sends that to your email and to the GitHub mobile app, as long as you're watching this repo (**Watch → Custom → Issues**) and have notifications for watched repos turned on. The issue closes itself when the item sells out again, and you can close it any time.

You get one alert per restock, not one every 5 minutes. The last known status of each item is kept in `state.json`.

### Optional: faster phone push with ntfy

1. Install the free **ntfy** app (iOS/Android) and subscribe to a topic name only you know, for example `restock-` followed by 12 random letters (anyone who knows the topic name can read it).
2. In this repo go to **Settings → Secrets and variables → Actions → New repository secret**, name it `NTFY_TOPIC`, and paste that topic name.

Alerts then also arrive as a high-priority push that opens the product page when tapped.

## Adding a store

A twice-daily Claude scheduled task searches for new legitimate sellers and adds any Shopify store pages it finds here automatically (commits titled "Add … to watchlist (found by daily search)"). It only adds pages whose title matches the item and whose price is under the ceiling, and it never changes or removes existing items.

To add one yourself, edit `watchlist.json` on GitHub and add an item:

| `type` | Use for | `check_url` |
|---|---|---|
| `shopify` | Shopify stores (most indie toy and card shops) | the product URL with `.js` on the end |
| `meta` | Pages with a `product:availability` meta tag (Steel City) | the product page |
| `jsonld` | Pages with schema.org product data (Midwest Cards) | the product page |
| `auto` | Any other store page: tries meta, schema.org, then microdata | the product page |

Quick Shopify test: open `https://store.com/products/some-product.js` in a browser. If you see product data with `"available": false/true`, it works.

Set `max_price` so reseller markups don't trigger an alert. Set `"enabled": false` to pause an item.

## What it can't watch

Amazon, Target, Walmart, Office Depot, Fanatics, Topps.com and Blowout Cards block automated checks from GitHub's servers, and Schylling's own store sits behind a bot check. For those, rely on TrackaLacker alerts, Amazon's invite, and the twice-daily Claude search.

## Things to know

- GitHub's 5-minute schedule can start a few minutes late when GitHub is busy.
- GitHub pauses scheduled workflows in a public repo after 60 days with no commits. Status changes commit to `state.json`, which counts, but if it goes quiet, re-enable it under **Actions → Restock watch**.
- If a store blocks or changes its page, that item shows "couldn't check" in the run summary and never sends a false alert.
- To stop everything: **Actions → Restock watch → ⋯ → Disable workflow**.

## Run it yourself

```
python3 -m unittest test_watch      # tests
DRY_RUN=1 python3 watch.py          # check now without opening issues or saving state
```
