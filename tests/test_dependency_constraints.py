# coding: utf-8
"""依赖版本上界不能被无意中放开。

`redis` extra 长期是 `redis>=5.0.0`（无上界），于是 pip 会装到最新。redis-py 8.x
改了两个默认值，实测：

    redis-py   protocol默认   默认重试次数
    5.2.1      2              0
    6.4.0      2              3
    7.4.0      2              3
    8.1.0      None           10      <- 异类

重试次数要紧，因为 RPC 请求用 `RPUSH` 发，而 **RPUSH 不幂等**：redis-py 的重试
包住的是「发送 + 读应答」，服务端已经收下、只是应答丢了的情况下，同一条 RPUSH
会被重发 —— 一次下单可能派发两次（#245，报告人给了确定性故障注入复现）。
服务端 0.3.29 起按 request_id 去重兜住了这条，但没有理由把默认重试抬到 10。

这条测试只钉「有上界」，不钉具体值：将来验证过 8.x 安全，改上界即可，但要改得
是**明知故犯**，而不是某次顺手删掉。
"""
import io
import os
import re
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYPROJECT = os.path.join(ROOT, "pyproject.toml")
README = os.path.join(ROOT, "README.md")


def _read(path):
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


class RedisUpperBoundTest(unittest.TestCase):
    def test_the_redis_extra_has_an_upper_bound(self):
        line = [l for l in _read(PYPROJECT).splitlines()
                if l.strip().startswith("redis = [")]
        self.assertTrue(line, "pyproject 里找不到 redis extra")
        self.assertIn("<", line[0],
                      "redis extra 没有上界了，pip 会装到 8.x："
                      "默认重试 10 次 + RPUSH 不幂等 = 可能重复下单（#245）")

    def test_the_bound_still_excludes_the_version_that_changed_the_defaults(self):
        spec = [l for l in _read(PYPROJECT).splitlines()
                if l.strip().startswith("redis = [")][0]
        match = re.search(r"<\s*(\d+)", spec)
        self.assertIsNotNone(match, "上界写法认不出来：%s" % spec)
        self.assertLessEqual(int(match.group(1)), 8,
                             "上界放到了 8 以上，而 8.x 正是把默认重试抬到 10 的版本")

    def test_the_reason_is_written_down_next_to_the_constraint(self):
        """光有数字没有理由，下一个人会以为是随手写的然后删掉。"""
        text = _read(PYPROJECT)
        head = text.split("redis = [")[0][-800:]
        self.assertIn("#245", head, "约束旁边没写为什么")


class ReadmeDocumentsTheConstraintsTest(unittest.TestCase):
    def test_readme_states_the_client_python_ceiling(self):
        self.assertIn("3.13", _read(README),
                      "README 没有写客户端 Python 的建议上限")

    def test_readme_warns_about_upgrading_redis_inside_qmt(self):
        """QMT 是 Python 3.6，redis-py 4.4 起要 3.7+，硬升会重演 #71。"""
        text = _read(README)
        self.assertIn("#71", text, "README 没提 QMT 端升 redis 的坑（#71）")


if __name__ == "__main__":
    unittest.main()
