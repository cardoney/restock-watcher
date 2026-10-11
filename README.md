# Restock watcher

Checks a short list of product pages every 5 minutes and alerts you the moment one comes back in stock at a sane price.

Currently watching:

- **Banana Ball 2026 Collector's Box** at the Savannah Bananas official shop ($149, Shopify feed)
- **Banana Ball 2026 Collector's Box** at Steel City Collectibles

## How you get alerted

When an item goes from out of stock to in stock (at or under its `max_price`), the watcher opens a GitHub issue titled **IN STOCK: …** that @mentions you and is assigned to you. GitHub sends that to your email and to the GitHub mobile app, as long as you're watching this repo (**Watch → Custom → Issues**) and have notifications for watched repos turned on. The issue closes itself when the item sells out again, and you can close it any time.

You get one alert per restock, not one every 5 minutes. The last known status of each item is kept in `state.json`.

### Optional: faster phone push with ntfy

1. Install the free **ntfy** app (iOS/Android) and subscribe to a topic name only you know, for example `restock-` followed by 12 random letters (anyone who knows the topic name can read it).
2. In this repo go to **Settings → Secrets and variables → Actions → New repository secret**, name it `NTFY_TOPIC`, and paste that topic name.

Alerts then also arrive as a high-priority push that opens the product page when tapped.

## Adding a store

Edit `watchlist.json` on GitHub and add an item:

| `type` | Use for | `check_url` |
|---|---|---|
| `shopify` | Shopify stores (most indie toy and card shops) | the product URL with `.js` on the end |
| `meta` | Pages with a `product:availability` meta tag (Steel City) | the product page |
| `jsonld` | Pages with schema.org product data | the product page |

Quick Shopify test: open `https://store.com/products/some-product.js` in a browser. If you see product data with `"available": false/true`, it works.

Set `max_price` so reseller markups don't trigger an alert. Set `"enabled": false` to pause an item.

## What it can't watch

Amazon, Target, Walmart and Office Depot block automated checks from GitHub's servers, and Schylling's own store sits behind a bot check. For those, rely on Amazon's invite, TrackaLacker alerts, and the daily Claude search.

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
