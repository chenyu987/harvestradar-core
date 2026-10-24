"""Prefilter rules that need no model. Run: python -m unittest discover tests"""

import unittest

from harvestradar.prefilter import prefilter, sure_request


class SureRequestTest(unittest.TestCase):
    def test_asks_for_an_item(self):
        for line in ["求一张女beta 3/14", "Looking for x1 male and x1 female alpha items",
                     "急！有没有女beta item 今晚", "need an alpha item this weekend", "想借一下物品"]:
            self.assertTrue(sure_request(f"Alex\n{line}\n10:02"), line)

    def test_not_a_item_request(self):
        for line in ["出女 beta 3/14", "求21號queens 去maple 的車", "人找车 周六 riverton", "beta 好用吗"]:
            self.assertFalse(sure_request(line), line)

    def test_rule_keeps_without_asking_the_model(self):
        # model "qwen3:none" does not exist: reaching it would be an error, not "rule"
        res = prefilter("unused.png", model="qwen3:none", ocr_text="Alex\n求一张男 alpha")
        self.assertEqual((res.keep, res.stage), (True, "rule"))


if __name__ == "__main__":
    unittest.main()
