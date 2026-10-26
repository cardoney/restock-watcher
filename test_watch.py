import json
import unittest

import watch


SHOPIFY_OUT = json.dumps({"available": False, "price": 14900,
                          "variants": [{"available": False, "price": 14900}]})
SHOPIFY_IN = json.dumps({"available": True, "price": 14900,
                         "variants": [{"available": True, "price": 14900}]})
STEEL_OUT = '<meta property="product:availability" content="out of stock" />' \
            '<meta property="product:price:amount" content="0.00" />'
STEEL_IN = '<meta content="in stock" property="product:availability">' \
           '<meta property="product:price:amount" content="159.99" />'
LD_IN = '<script type="application/ld+json">{"@type":"Product","offers":' \
        '{"@type":"Offer","price":"29.99","availability":"https://schema.org/InStock"}}</script>'
LD_OUT = '<script type="application/ld+json">{"@graph":[{"@type":"Product","offers":' \
         '[{"price":"29.99","availability":"http://schema.org/OutOfStock"}]}]}</script>'
LD_PRE = '<script type="application/ld+json">{"offers":{"price":"34.99",' \
         '"availability":"https://schema.org/PreOrder"}}</script>'


class Parsers(unittest.TestCase):
    def test_shopify(self):
        self.assertEqual(watch.parse_shopify(SHOPIFY_OUT)[:2], (False, 149.0))
        self.assertEqual(watch.parse_shopify(SHOPIFY_IN)[:2], (True, 149.0))

    def test_meta(self):
        self.assertEqual(watch.parse_meta(STEEL_OUT)[:2], (False, None))
        self.assertEqual(watch.parse_meta(STEEL_IN)[:2], (True, 159.99))
        self.assertIsNone(watch.parse_meta("<html></html>")[0])

    def test_jsonld(self):
        self.assertEqual(watch.parse_jsonld(LD_IN)[:2], (True, 29.99))
        self.assertEqual(watch.parse_jsonld(LD_OUT)[0], False)
        self.assertEqual(watch.parse_jsonld(LD_PRE)[:2], (True, 34.99))
        self.assertIsNone(watch.parse_jsonld("<html></html>")[0])

    def test_jsonld_bigcommerce_shape(self):
        # Midwest Cards (BigCommerce) real offer shape
        page = ('<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product",'
                '"name":"2026 Topps Finest Baseball Mega Box","offers":{"@type":"Offer","sku":"26TOBF-MB(B)",'
                '"price":"154.95","priceCurrency":"USD","availability":"https://schema.org/OutOfStock"}}</script>'
                '<script type="application/ld+json">{"@type":"BreadcrumbList","itemListElement":'
                '[{"@type":"ListItem","position":1,"name":"Baseball Cards"}]}</script>')
        self.assertEqual(watch.parse_jsonld(page)[:2], (False, 154.95))
        self.assertEqual(watch.parse_jsonld(page.replace("OutOfStock", "InStock"))[:2], (True, 154.95))

    def test_jsonld_woocommerce_shape(self):
        # Village Candy Shop (WooCommerce) real offer shape: price also nested in priceSpecification
        page = ('<script type="application/ld+json">{"@context":"https://schema.org/","@graph":['
                '{"@type":"BreadcrumbList","itemListElement":[{"@type":"ListItem","position":1,'
                '"item":{"name":"Home","@id":"https://villagecandyshop.com"}}]},'
                '{"@type":"Product","name":"Cream Soda Dum Dums (35 Count)","sku":929,"offers":[{"@type":"Offer",'
                '"priceSpecification":{"price":"14.95","priceCurrency":"USD","valueAddedTaxIncluded":"false"},'
                '"availability":"https://schema.org/OutOfStock","price":"14.95","priceCurrency":"USD"}]}]}</script>')
        self.assertEqual(watch.parse_jsonld(page)[:2], (False, 14.95))
        self.assertEqual(watch.parse_jsonld(page.replace("OutOfStock", "InStock"))[:2], (True, 14.95))

    def test_microdata(self):
        page = '<link itemprop="availability" href="https://schema.org/InStock"><meta itemprop="price" content="99.99">'
        self.assertEqual(watch.parse_microdata(page)[:2], (True, 99.99))
        self.assertEqual(watch.parse_microdata(page.replace("InStock", "OutOfStock"))[0], False)
        self.assertIsNone(watch.parse_microdata("<html></html>")[0])

    def test_auto_falls_through(self):
        self.assertEqual(watch.parse_auto(STEEL_IN)[:2], (True, 159.99))
        self.assertEqual(watch.parse_auto(LD_IN)[:2], (True, 29.99))
        self.assertEqual(watch.parse_auto('<span itemprop="availability" content="OutOfStock"></span>')[0], False)
        self.assertIsNone(watch.parse_auto("<html>Add to cart</html>")[0])


class Logic(unittest.TestCase):
    def test_price_drop_on_in_stock_item_alerts(self):
        # in stock at $149.95 (over $100) -> no alert; then price drops to $99.95 -> alert
        self.assertEqual(watch.classify(True, 149.95, 100), watch.PRICEY)
        self.assertEqual(watch.decide(watch.PRICEY, watch.classify(True, 99.95, 100)), (watch.IN, True, False))

    def test_classify_price_ceiling(self):
        self.assertEqual(watch.classify(True, 199.0, 175), watch.PRICEY)
        self.assertEqual(watch.classify(True, 149.0, 175), watch.IN)
        self.assertEqual(watch.classify(True, None, 175), watch.IN)
        self.assertEqual(watch.classify(False, 149.0, 175), watch.OUT)
        self.assertEqual(watch.classify(None, None, 175), watch.UNKNOWN)

    def test_decide_transitions(self):
        # first run, in stock -> alert
        self.assertEqual(watch.decide(None, watch.IN), (watch.IN, True, False))
        # stays in stock -> no repeat alert
        self.assertEqual(watch.decide(watch.IN, watch.IN), (watch.IN, False, False))
        # goes out -> close alert
        self.assertEqual(watch.decide(watch.IN, watch.OUT), (watch.OUT, False, True))
        # blocked fetch never changes state
        self.assertEqual(watch.decide(watch.IN, watch.UNKNOWN), (watch.IN, False, False))
        self.assertEqual(watch.decide(watch.OUT, watch.UNKNOWN), (watch.OUT, False, False))
        # over max price doesn't alert
        self.assertEqual(watch.decide(watch.OUT, watch.PRICEY), (watch.PRICEY, False, False))

    def test_check_item_handles_blocks(self):
        item = {"id": "x", "type": "meta", "check_url": "u", "max_price": 175}
        self.assertEqual(watch.check_item(item, lambda u: (403, ""))["status"], watch.UNKNOWN)
        def boom(u):
            raise TimeoutError("slow")
        self.assertEqual(watch.check_item(item, boom)["status"], watch.UNKNOWN)
        self.assertEqual(watch.check_item(item, lambda u: (200, STEEL_IN))["status"], watch.IN)

    def test_health_tracking(self):
        from datetime import datetime, timezone
        e = {"status": watch.OUT}
        self.assertTrue(watch.track_readability(e, watch.UNKNOWN, "2026-10-23T10:00:00Z"))
        self.assertFalse(watch.track_readability(e, watch.UNKNOWN, "2026-10-23T10:05:00Z"))
        self.assertEqual(e["unreadable_since"], "2026-10-23T10:00:00Z")  # keeps the first failure time
        items = [{"id": "a", "name": "A", "product_url": "u"}]
        state = {"a": e}
        early = datetime(2026, 10, 23, 11, 0, tzinfo=timezone.utc)
        late = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(watch.unreadable_items(items, state, early), [])
        self.assertEqual(len(watch.unreadable_items(items, state, late)), 1)
        self.assertTrue(watch.track_readability(e, watch.OUT, "2026-10-23T12:05:00Z"))
        self.assertNotIn("unreadable_since", e)
        self.assertEqual(watch.unreadable_items(items, state, late), [])

    def test_watchlist_is_valid(self):
        data = json.loads(watch.WATCHLIST.read_text())
        ids = [i["id"] for i in data["items"]]
        self.assertEqual(len(ids), len(set(ids)))
        for i in data["items"]:
            self.assertIn(i["type"], watch.PARSERS)
            self.assertTrue(i["check_url"].startswith("https://"))


if __name__ == "__main__":
    unittest.main()
