import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.code_utils import (
    is_bond_code,
    min_lot,
    normalize_stock_code,
    round_buy_volume,
    round_sell_volume,
)
from bigqmt_signal_trader.price_engine import _price_precision


class CodeUtilsTest(unittest.TestCase):
    def test_normalize_stock_code_accepts_common_formats(self):
        self.assertEqual(normalize_stock_code("600000"), "600000.SH")
        self.assertEqual(normalize_stock_code("000001"), "000001.SZ")
        self.assertEqual(normalize_stock_code("SZ000001"), "000001.SZ")
        self.assertEqual(normalize_stock_code("sh600000"), "600000.SH")
        self.assertEqual(normalize_stock_code("600000.SH"), "600000.SH")

    def test_normalize_stock_code_keeps_etf_tradable(self):
        self.assertEqual(normalize_stock_code("510300"), "510300.SH")
        self.assertEqual(normalize_stock_code("159915"), "159915.SZ")

    def test_round_buy_volume_by_lot(self):
        self.assertEqual(round_buy_volume("000001.SZ", 1234), 1200)
        self.assertEqual(round_buy_volume("688001.SH", 234), 200)

    def test_round_sell_volume_keeps_all_when_sell_all(self):
        self.assertEqual(round_sell_volume("000001.SZ", 1234, sell_all=False), 1200)
        self.assertEqual(round_sell_volume("000001.SZ", 1234, sell_all=True), 1234)


class ConvertibleBondTest(unittest.TestCase):
    """可转债 / 可交换债：10 张起、0.001 报价，沪市转债不能判到深市。"""

    SH_BONDS = ("110043", "111000", "113050", "118076", "132018")
    SZ_BONDS = ("123281", "127045", "128136")

    def test_shanghai_bonds_are_not_misfiled_to_shenzhen(self):
        # 沪市转债是 "1" 开头，落不进 ("5","6") 那条规则
        for code in self.SH_BONDS:
            self.assertEqual(normalize_stock_code(code), code + ".SH", code)

    def test_shenzhen_bonds_stay_in_shenzhen(self):
        for code in self.SZ_BONDS:
            self.assertEqual(normalize_stock_code(code), code + ".SZ", code)

    def test_bond_codes_are_recognised_in_every_form(self):
        for raw in ("113050", "113050.SH", "SH113050", "sh113050"):
            self.assertTrue(is_bond_code(raw), raw)
        for raw in ("600000.SH", "000001.SZ", "688981.SH", "510300.SH", "159915.SZ"):
            self.assertFalse(is_bond_code(raw), raw)

    def test_min_lot_is_ten_for_bonds(self):
        for code in self.SH_BONDS + self.SZ_BONDS:
            self.assertEqual(min_lot(code), 10, code)

    def test_one_lot_of_bonds_survives_rounding(self):
        # 这是修复的根本原因：(10 // 100) * 100 == 0，一手转债的单子直接废掉
        self.assertEqual(round_buy_volume("113050.SH", 10), 10)
        self.assertEqual(round_buy_volume("113050.SH", 37), 30)
        self.assertEqual(round_buy_volume("113050.SH", 9), 0)

    def test_bond_sell_rounds_by_ten_and_keeps_odd_lots_on_sell_all(self):
        self.assertEqual(round_sell_volume("113050.SH", 37), 30)
        self.assertEqual(round_sell_volume("113050.SH", 7, sell_all=True), 7)

    def test_bond_price_precision_is_three_decimals(self):
        for code in self.SH_BONDS + self.SZ_BONDS:
            self.assertEqual(_price_precision(code), 3, code)

    def test_existing_instruments_keep_their_precision(self):
        self.assertEqual(_price_precision("600000.SH"), 2)
        self.assertEqual(_price_precision("688981.SH"), 2)
        self.assertEqual(_price_precision("510300.SH"), 3)
        self.assertEqual(_price_precision("159915.SZ"), 3)


class UnchangedBehaviourTest(unittest.TestCase):
    """非债券品种的行为一个字都不能变。"""

    def test_stock_and_star_lots_are_untouched(self):
        self.assertEqual(min_lot("000001.SZ"), 100)
        self.assertEqual(min_lot("600000.SH"), 100)
        self.assertEqual(min_lot("688001.SH"), 200)
        self.assertEqual(min_lot("510300.SH"), 100)

    def test_ordinary_code_market_inference_is_untouched(self):
        self.assertEqual(normalize_stock_code("600000"), "600000.SH")
        self.assertEqual(normalize_stock_code("000001"), "000001.SZ")
        self.assertEqual(normalize_stock_code("510300"), "510300.SH")
        self.assertEqual(normalize_stock_code("159915"), "159915.SZ")


if __name__ == "__main__":
    unittest.main()
