import glob
import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
SRC = os.path.join(ROOT, "src")


class EntryEncodingTest(unittest.TestCase):
    """Guard against the QMT load crash: a file declaring ``#coding:gbk`` but
    containing non-GBK (e.g. UTF-8 Chinese) bytes fails to load under QMT's
    GBK-based Python with 'gbk codec can't decode ...'. Entry files loaded by the
    QMT editor must stay GBK-decodable (ASCII is the safe subset)."""

    def test_gbk_declared_files_are_gbk_decodable(self):
        bad = []
        for path in glob.glob(os.path.join(SRC, "**", "*.py"), recursive=True):
            if "__pycache__" in path:
                continue
            data = open(path, "rb").read()
            first_line = data.split(b"\n", 1)[0].lower().replace(b" ", b"")
            if b"coding:gbk" not in first_line and b"coding=gbk" not in first_line:
                continue
            try:
                data.decode("gbk")
            except UnicodeDecodeError as exc:
                bad.append("%s (byte %d)" % (os.path.relpath(path, ROOT), exc.start))
        self.assertEqual(
            bad,
            [],
            "files declare #coding:gbk but are not GBK-decodable; QMT will fail to load them: %s" % bad,
        )

    def test_qmt_loader_stops_previous_service_before_clearing_modules(self):
        """A QMT strategy restart must release the old ZMQ port first."""
        path = os.path.join(SRC, "BIGQMT_REDIS_DRYRUN.py")
        with open(path, "r", encoding="gbk") as source_file:
            source = source_file.read()
        self.assertIn("def _stop_previous_rpc_service():", source)
        stop_call = source.index("\n_stop_previous_rpc_service()\n")
        clear_call = source.index("\n_clear_local_modules()\n")
        self.assertLess(
            stop_call,
            clear_call,
        )


if __name__ == "__main__":
    unittest.main()
