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


class Logic(unittest.TestCase):
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

    def test_watchlist_is_valid(self):
        data = json.loads(watch.WATCHLIST.read_text())
        ids = [i["id"] for i in data["items"]]
        self.assertEqual(len(ids), len(set(ids)))
        for i in data["items"]:
            self.assertIn(i["type"], watch.PARSERS)
            self.assertTrue(i["check_url"].startswith("https://"))


if __name__ == "__main__":
    unittest.main()
