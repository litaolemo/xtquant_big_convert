import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider


class RawMarketContext:
    def __init__(self, payload=None):
        self.payload = payload or {}
        self.calls = []

    def get_market_data_ex_ori(
        self,
        fields=None,
        stock_code=None,
        period="1d",
        start_time="",
        end_time="",
        count=-1,
        dividend_type="none",
    ):
        self.calls.append(
            {
                "fields": fields,
                "stock_code": stock_code,
                "period": period,
                "start_time": start_time,
                "end_time": end_time,
                "count": count,
                "dividend_type": dividend_type,
            }
        )
        return self.payload

    def get_market_data_ex(self, *args, **kwargs):
        raise AssertionError("DataFrame-producing QMT API must not be called")


class BigQmtRawMarketBridgeTest(unittest.TestCase):
    def test_market_data_ex_uses_raw_context_api(self):
        rows = [[1784014200000, 55.1], [1784014260000, 55.2]]
        context = RawMarketContext({"600276.SH": rows})
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(
            field_list=["close"], stock_list=["600276.SH"], period="1m", count=2
        )

        self.assertEqual("DataFrame", data["600276.SH"]["__bigqmt_type__"])
        self.assertEqual(["stime", "close"], data["600276.SH"]["columns"])
        self.assertEqual(rows, data["600276.SH"]["records"])
        self.assertEqual(["close"], context.calls[0]["fields"])
        self.assertEqual(["600276.SH"], context.calls[0]["stock_code"])

    def test_market_data_ex_returns_empty_frame_for_requested_symbol(self):
        context = RawMarketContext({})
        provider = BigQmtMarketDataProvider(context)

        data = provider.get_market_data_ex(
            field_list=["close"], stock_list=["600276.SH"], period="1m", count=2
        )

        self.assertEqual([], data["600276.SH"]["records"])
        self.assertEqual(["stime", "close"], data["600276.SH"]["columns"])


if __name__ == "__main__":
    unittest.main()
