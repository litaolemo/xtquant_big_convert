"""A pandas Panel must survive the RPC (issue #115).

QMT ships pandas 0.22, where ``get_financial_data`` with several stocks AND
several dates returns a Panel -- the 3-D container pandas removed in 1.0. A
Panel has no ``.columns`` and no ``.index``, so it fell past to_jsonable's
DataFrame and Series branches to the ``__dict__`` fallback, and
``vars(panel)`` is ``{'_data': ..., 'is_copy': None}``. After the
underscore filter that leaves ``{'is_copy': None}``.

So the caller got no error and no data -- just the one public attribute the
object happened to have. One stock, or one date, returns a DataFrame and
worked fine, which is why this hid for so long.

The client runs modern pandas and cannot rebuild a Panel, so it comes back as
``{item: DataFrame}``.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.redis_rpc import to_jsonable
from bigqmt_signal_trader.xtquant_compat import _restore_jsonable


class FakePanel(object):
    """pandas 0.22's Panel, to the extent to_jsonable can see it.

    Written by hand because pandas >= 1.0 has no Panel to import, and this test
    has to run on the client's pandas as well as QMT's.
    """

    def __init__(self, frames, major_axis, minor_axis):
        self._frames = frames                      # {item: FakeFrame}
        self.items = list(frames.keys())
        self.major_axis = list(major_axis)
        self.minor_axis = list(minor_axis)
        self.is_copy = None                        # the attribute that leaked

    def __getitem__(self, item):
        return self._frames[item]


class FakeFrame(object):
    """Enough of a DataFrame for to_jsonable's duck-typed branch."""

    def __init__(self, records, columns):
        self._records = records
        self.columns = columns
        self.index = list(range(len(records)))

    def reset_index(self):
        return self

    def to_dict(self, orient=None):
        return list(self._records)


def _panel():
    columns = ["total_capital", "circulating_capital"]
    return FakePanel(
        {
            "000001.SZ": FakeFrame(
                [{"total_capital": 1.0, "circulating_capital": 2.0}], columns),
            "000017.SZ": FakeFrame(
                [{"total_capital": 3.0, "circulating_capital": 4.0}], columns),
        },
        major_axis=["20260101", "20260830"],
        minor_axis=columns,
    )


class ServerSideTest(unittest.TestCase):
    def test_a_panel_is_no_longer_reduced_to_is_copy(self):
        payload = to_jsonable(_panel())

        self.assertNotEqual(payload, {"is_copy": None})
        self.assertEqual(payload["__bigqmt_type__"], "Panel")

    def test_every_item_is_carried(self):
        payload = to_jsonable(_panel())

        self.assertEqual(sorted(payload["data"]), ["000001.SZ", "000017.SZ"])

    def test_each_item_is_a_dataframe_payload(self):
        payload = to_jsonable(_panel())
        first = payload["data"]["000001.SZ"]

        self.assertEqual(first["__bigqmt_type__"], "DataFrame")
        self.assertEqual(first["records"],
                         [{"total_capital": 1.0, "circulating_capital": 2.0}])

    def test_the_axes_ride_along(self):
        """So a caller can tell how the cube was sliced."""
        payload = to_jsonable(_panel())

        self.assertEqual(payload["major_axis"], ["20260101", "20260830"])
        self.assertEqual(payload["minor_axis"],
                         ["total_capital", "circulating_capital"])

    def test_a_dict_is_not_mistaken_for_a_panel(self):
        """dict has .items too -- only the axes tell them apart."""
        payload = to_jsonable({"items": 1, "a": 2})

        self.assertEqual(payload, {"items": 1, "a": 2})

    def test_a_broken_panel_does_not_take_the_rpc_down(self):
        class Exploding(FakePanel):
            def __getitem__(self, item):
                raise RuntimeError("no data")

        payload = to_jsonable(Exploding({"a": None}, [], []))

        self.assertIsInstance(payload, str)


class RoundTripTest(unittest.TestCase):
    """What the caller actually ends up with."""

    def _restored(self):
        return _restore_jsonable(to_jsonable(_panel()))

    def test_it_is_a_dict_keyed_by_item(self):
        restored = self._restored()

        self.assertEqual(sorted(restored), ["000001.SZ", "000017.SZ"])

    def test_the_values_are_dataframes(self):
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("pandas not installed")

        restored = self._restored()

        self.assertIsInstance(restored["000001.SZ"], pd.DataFrame)
        self.assertEqual(list(restored["000001.SZ"]["total_capital"]), [1.0])

    def test_a_dataframe_still_round_trips_unchanged(self):
        """The new branch sits above the DataFrame one; make sure it did not
        swallow the two-dimensional case."""
        payload = to_jsonable(FakeFrame([{"a": 1}], ["a"]))

        self.assertEqual(payload["__bigqmt_type__"], "DataFrame")


if __name__ == "__main__":
    unittest.main()
