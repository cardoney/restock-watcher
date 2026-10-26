# Restock watcher

Checks a short list of product pages every 5 minutes and alerts you the moment one comes back in stock at a sane price.

Currently watching:

| Item | Alert at or under | Stores |
|---|---|---|
| Banana Ball 2026 Collector's Box | $200 | Savannah Bananas official shop |
| 2026 Topps Finest Baseball Mega Box | $100 | Giant Sports Cards (2 listings), Midwest Cards |
| NeeDoh Squishmas Advent Calendar 2026 | $45 | Learning Express Wilmington, Fat Brain Toys |
| Dum Dums Cream Soda single-flavor bag | $33 | Spangler Candy (official), Village Candy Shop, Candy Warehouse, Candy Me Up |

New-listing searches also watch Giant Sports Cards, the Savannah Bananas shop, Learning Express Wilmington, Spangler and Candy Warehouse for the same item turning up under a new listing (see below).

Steel City Collectibles, Dave & Adam's, Topps.com and GameStop block checks from GitHub's servers (their entries are disabled in `watchlist.json`). Fanatics, Target, Walmart and Blowout Cards also block automated checks; those are the stores that sell the Topps Finest Mega Box at its $69.99 list price. The twice-daily Claude search covers all of them.

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
| `shopify_search` | New listings of the item at a Shopify store (see below) | the store's `/search/suggest.json` search |

Quick Shopify test: open `https://store.com/products/some-product.js` in a browser. If you see product data with `"available": false/true`, it works.

### Watching for new listings

Stores sometimes restock under a brand-new listing (Giant Sports Cards' second Topps Finest listing ends in `-2`), which a product-page check never sees. A `shopify_search` item searches the store instead, using Shopify's public predictive search, and alerts when a listing whose title matches is in stock at or under `max_price`. Listings already in `watchlist.json` are skipped, so you never get two alerts for the same page, and the alert links straight to the listing it found.

```json
{
  "id": "topps-finest-mega-giant-search",
  "name": "2026 Topps Finest Baseball Mega Box - Giant Sports Cards (new listings)",
  "product_url": "https://giantsportscards.com/search?q=%22finest+baseball+mega%22&type=product",
  "type": "shopify_search",
  "check_url": "https://giantsportscards.com/search/suggest.json?q=%22finest+baseball+mega%22&resources%5Btype%5D=product&resources%5Blimit%5D=10&resources%5Boptions%5D%5Bunavailable_products%5D=last",
  "match": ["2026", "finest", "baseball", "mega"],
  "exclude": ["case", "hobby", "delight", "breaker", "first day", "lot"],
  "max_price": 100,
  "enabled": true
}
```

- Put the search phrase in quotes (`%22...%22`) in `q`; unquoted searches match loosely and can push the real item out of the 10 results.
- `match`: every word must appear in the listing title. `exclude`: none may (keep hobby boxes, cases and single packs out).
- Test it by opening the `check_url` in a browser: matching listings show `"available": true/false`.

Set `max_price` so reseller markups don't trigger an alert. Set `"enabled": false` to pause an item.

## What it can't watch

Amazon, Target, Walmart, Office Depot, Fanatics, Topps.com and Blowout Cards block automated checks from GitHub's servers, and Schylling's own store sits behind a bot check. For those, rely on TrackaLacker alerts, Amazon's invite, and the twice-daily Claude search.

## Things to know

- If a store page stays unreadable for 2+ hours, the watcher opens one **Watcher can't read …** issue so a blocked store never fails silently. It closes itself when the page reads again.
- GitHub treats the 5-minute schedule as best effort. On this repo it ran only every 3–5 hours in October 2026, so without an external trigger, treat this as a check every few hours.
- GitHub pauses scheduled workflows in a public repo after 60 days with no commits. Status changes commit to `state.json`, which counts, but if it goes quiet, re-enable it under **Actions → Restock watch**.
- If a store blocks or changes its page, that item shows "couldn't check" in the run summary and never sends a false alert.
- To stop everything: **Actions → Restock watch → ⋯ → Disable workflow**.

## Run it yourself

```
python3 -m unittest test_watch      # tests
DRY_RUN=1 python3 watch.py          # check now without opening issues or saving state
```
