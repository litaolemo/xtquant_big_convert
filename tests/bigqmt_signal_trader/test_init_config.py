"""The setup wizard (`bigqmt-init`).

Deployment previously meant copying two .example.py files and knowing which of
~30 keys matter. The wizard asks for the handful that vary and derives the rest,
so the thing worth pinning is that what it derives is actually loadable and that
the server and client blocks agree with each other.

Prompting is driven through injected read/write callables, so these run without
a TTY -- including the password prompt, which would otherwise go straight to
getpass and hang.
"""

import io
import os
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import init_config
from bigqmt_signal_trader.transports.zmq_transport import (
    DEFAULT_ZMQ_HOST,
    _default_zmq_address,
    _default_zmq_port,
)


class _Script(object):
    """Feeds canned answers; records what was asked."""

    def __init__(self, answers, secrets=None):
        self.answers = list(answers)
        self.secrets = list(secrets or [])
        self.prompts = []
        self.output = []

    def read(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else ""

    def read_secret(self, prompt):
        self.prompts.append(prompt)
        return self.secrets.pop(0) if self.secrets else ""

    def write(self, text):
        self.output.append(text)

    def text(self):
        return "".join(self.output)


def _answers(**overrides):
    base = dict(init_config.DEFAULTS)
    base.update({"account_id": "8886800503", "account_type": "STOCK",
                 "transport": "redis", "host": "10.0.0.5", "port": 6380,
                 "db": 7, "username": "svc", "password": "s3cret",
                 "allow_order_methods": False, "deployment": "package"})
    base.update(overrides)
    return base


def _load(source, name):
    """Exec generated config source the way the real loader would."""
    namespace = {}
    exec(compile(source, name, "exec"), namespace)
    return namespace


class RenderedConfigTest(unittest.TestCase):
    def test_server_config_is_valid_python_carrying_the_answers(self):
        loaded = _load(init_config.render_server_config(_answers()), "local_config.py")

        self.assertEqual(loaded["BIGQMT_ACCOUNT_ID"], "8886800503")
        self.assertEqual(loaded["BIGQMT_ACCOUNT_TYPE"], "STOCK")
        self.assertEqual(loaded["BIGQMT_REDIS_CONFIG"]["host"], "10.0.0.5")
        self.assertEqual(loaded["BIGQMT_REDIS_CONFIG"]["port"], 6380)
        self.assertEqual(loaded["BIGQMT_REDIS_CONFIG"]["db"], 7)
        self.assertEqual(loaded["BIGQMT_REDIS_CONFIG"]["password"], "s3cret")

    def test_client_config_is_valid_python_carrying_the_answers(self):
        loaded = _load(init_config.render_client_config(_answers()), "client_config.py")

        self.assertEqual(loaded["BIGQMT_ACCOUNT_ID"], "8886800503")
        self.assertEqual(loaded["BIGQMT_REDIS_CONFIG"]["transport"], "redis")
        self.assertEqual(loaded["BIGQMT_REDIS_CONFIG"]["host"], "10.0.0.5")
        self.assertIs(loaded["BIGQMT_LOCAL_CACHE_CONFIG"]["fallback_rpc"], True)

    def test_server_and_client_agree_on_the_connection(self):
        """Two files, one deployment: a mismatch here is a silent no-connect."""
        answers = _answers()
        server = _load(init_config.render_server_config(answers), "s.py")
        client = _load(init_config.render_client_config(answers), "c.py")

        self.assertEqual(server["BIGQMT_ACCOUNT_ID"], client["BIGQMT_ACCOUNT_ID"])
        for key in ("host", "port", "db", "username", "password"):
            self.assertEqual(server["BIGQMT_REDIS_CONFIG"][key],
                             client["BIGQMT_REDIS_CONFIG"][key], key)

    def test_order_rpc_is_off_unless_asked_for(self):
        off = _load(init_config.render_server_config(_answers()), "s.py")
        self.assertIs(off["BIGQMT_REDIS_CONFIG"]["rpc_allow_order_methods"], False)

        on = _load(init_config.render_server_config(
            _answers(allow_order_methods=True)), "s.py")
        self.assertIs(on["BIGQMT_REDIS_CONFIG"]["rpc_allow_order_methods"], True)

    def test_background_threads_follow_the_transport(self):
        """Not a safety choice, a latency one -- and it differs per transport.

        This test used to pin False for everything, matching a blanket rule
        that was protecting the wrong thing: safety comes from
        LISTENER_DEFERRED_METHODS being excluded when the listener list is
        expanded (test_listener_methods_never_leak_deferred), not from this
        flag. Pinning one value shipped zmq 37x slower than it needed to be
        (592.9ms vs 15.8ms) and redis 9x (30.7ms vs 3.4ms) -- see #244 and
        docs/LATENCY_REPORT.md.
        """
        wants_background = {"redis": True, "zmq": False,
                            "pipe": False, "mysql": False}
        for transport, expected in sorted(wants_background.items()):
            loaded = _load(init_config.render_server_config(
                _answers(transport=transport)), "s.py")
            self.assertIs(
                loaded["BIGQMT_REDIS_CONFIG"]["rpc_background_threads"], expected,
                "transport=%s 应该是 %s" % (transport, expected))
            self.assertIs(loaded["BIGQMT_REDIS_CONFIG"]["rpc_process_in_listener"], True)

    def test_the_order_switch_does_not_change_the_threading_mode(self):
        for answers in (_answers(), _answers(allow_order_methods=True)):
            loaded = _load(init_config.render_server_config(answers), "s.py")
            self.assertIs(loaded["BIGQMT_REDIS_CONFIG"]["rpc_background_threads"], True)

    def test_zmq_client_gets_a_concrete_connect_address(self):
        loaded = _load(init_config.render_client_config(
            _answers(transport="zmq", host="192.168.1.9")), "c.py")

        zmq = loaded["BIGQMT_REDIS_CONFIG"]["zmq"]
        address = zmq["connect_address"]
        self.assertTrue(address.startswith("tcp://192.168.1.9:"), address)
        self.assertEqual(zmq["host"], "192.168.1.9")
        self.assertEqual(zmq["port"], _default_zmq_port("8886800503"))

    def test_zmq_client_address_matches_the_server_transport_default(self):
        """The generated client must dial the endpoint ZmqTransport binds."""
        for account_id in ("8886800503", "account-without-digits"):
            loaded = _load(init_config.render_client_config(
                _answers(account_id=account_id, transport="zmq",
                         host="192.168.1.9")), "c.py")

            address = loaded["BIGQMT_REDIS_CONFIG"]["zmq"]["connect_address"]
            self.assertEqual(
                address,
                _default_zmq_address(account_id, host="192.168.1.9"),
            )

    def test_zmq_same_host_configs_use_the_same_loopback_endpoint(self):
        answers = _answers(
            account_id="8886800503", transport="zmq", host=DEFAULT_ZMQ_HOST
        )
        server = _load(init_config.render_server_config(answers), "s.py")
        client = _load(init_config.render_client_config(answers), "c.py")

        expected = _default_zmq_address("8886800503", host=DEFAULT_ZMQ_HOST)
        self.assertEqual(
            server["BIGQMT_REDIS_CONFIG"]["zmq"]["bind_address"], expected
        )
        self.assertEqual(
            client["BIGQMT_REDIS_CONFIG"]["zmq"]["connect_address"], expected
        )

    def test_zmq_remote_host_connects_to_qmt_and_server_binds_all_interfaces(self):
        answers = _answers(
            account_id="8886800503", transport="zmq", host="192.168.8.13"
        )
        server = _load(init_config.render_server_config(answers), "s.py")
        client = _load(init_config.render_client_config(answers), "c.py")
        port = _default_zmq_port("8886800503")

        self.assertEqual(
            client["BIGQMT_REDIS_CONFIG"]["zmq"]["connect_address"],
            "tcp://192.168.8.13:%d" % port,
        )
        self.assertEqual(
            server["BIGQMT_REDIS_CONFIG"]["zmq"]["bind_address"],
            "tcp://0.0.0.0:%d" % port,
        )

    def test_credentials_survive_characters_that_would_break_naive_quoting(self):
        nasty = "a'b\"c\\d#e"
        loaded = _load(init_config.render_server_config(
            _answers(password=nasty, username=nasty)), "s.py")

        self.assertEqual(loaded["BIGQMT_REDIS_CONFIG"]["password"], nasty)
        self.assertEqual(loaded["BIGQMT_REDIS_CONFIG"]["username"], nasty)

    def test_generated_files_warn_against_committing_them(self):
        for render in (init_config.render_server_config,
                       init_config.render_client_config):
            self.assertIn("DO NOT COMMIT", render(_answers()))


class PromptFlowTest(unittest.TestCase):
    def test_a_full_redis_run_collects_every_answer(self):
        script = _Script(
            answers=["8886800503", "1", "1", "10.0.0.5", "6380", "7", "svc",
                     "n", "1", "", ""],
            secrets=["s3cret"])
        answers = init_config.prompt_answers(
            script.write, script.read, read_secret=script.read_secret)

        self.assertEqual(answers["account_id"], "8886800503")
        self.assertEqual(answers["account_type"], "STOCK")
        self.assertEqual(answers["transport"], "redis")
        self.assertEqual(answers["host"], "10.0.0.5")
        self.assertEqual(answers["port"], 6380)
        self.assertEqual(answers["db"], 7)
        self.assertEqual(answers["username"], "svc")
        self.assertEqual(answers["password"], "s3cret")
        self.assertIs(answers["allow_order_methods"], False)
        self.assertEqual(answers["deployment"], "package")

    def test_empty_answers_take_the_defaults(self):
        script = _Script(answers=["8886800503"] + [""] * 12)
        answers = init_config.prompt_answers(
            script.write, script.read, read_secret=script.read_secret)

        self.assertEqual(answers["account_type"], "STOCK")
        self.assertEqual(answers["transport"], "redis")
        self.assertEqual(answers["host"], "127.0.0.1")
        self.assertEqual(answers["port"], 6379)
        self.assertIs(answers["allow_order_methods"], False)

    def test_account_id_is_required(self):
        script = _Script(answers=["", "  ", "8886800503"] + [""] * 12)
        answers = init_config.prompt_answers(
            script.write, script.read, read_secret=script.read_secret)

        self.assertEqual(answers["account_id"], "8886800503")

    def test_a_bad_port_is_re_asked_rather_than_silently_zeroed(self):
        script = _Script(
            answers=["8886800503", "", "", "", "not-a-port", "6380"] + [""] * 8)
        answers = init_config.prompt_answers(
            script.write, script.read, read_secret=script.read_secret)

        self.assertEqual(answers["port"], 6380)

    def test_zmq_skips_the_redis_credential_prompts(self):
        script = _Script(answers=["8886800503", "", "2", "10.0.0.5"] + [""] * 8)
        answers = init_config.prompt_answers(
            script.write, script.read, read_secret=script.read_secret)

        self.assertEqual(answers["transport"], "zmq")
        self.assertEqual(answers["password"], "")
        self.assertNotIn("Redis 密码", "".join(script.prompts))
        self.assertIn("0.0.0.0", script.text())
        self.assertIn("防火墙", script.text())

    def test_no_redis_single_file_forces_zmq(self):
        """Choosing a build that cannot import redis must not leave the config
        claiming a redis transport."""
        script = _Script(
            answers=["8886800503", "", "1", "", "", "", "", "n", "3", "", ""],
            secrets=[""])
        answers = init_config.prompt_answers(
            script.write, script.read, read_secret=script.read_secret)

        self.assertEqual(answers["deployment"], "single_file_no_redis")
        self.assertEqual(answers["transport"], "zmq")

    def test_the_password_prompt_says_it_is_hidden(self):
        script = _Script(answers=["8886800503"] + [""] * 12)
        init_config.prompt_answers(
            script.write, script.read, read_secret=script.read_secret)

        self.assertIn("不回显", "".join(script.prompts))

    def test_enabling_order_rpc_is_preceded_by_a_warning(self):
        script = _Script(answers=["8886800503"] + [""] * 12)
        init_config.prompt_answers(
            script.write, script.read, read_secret=script.read_secret)

        self.assertIn("任何能连上这条通道的程序都可以下单", script.text())


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _answers(self, **overrides):
        overrides.setdefault("qmt_python_dir", self.tmp)
        overrides.setdefault("client_dir", self.tmp)
        return _answers(**overrides)

    def test_writes_both_config_files(self):
        script = _Script(answers=[])
        written = init_config.apply(
            self._answers(), ROOT, script.write, script.read, force=True)

        self.assertEqual(len(written), 2)
        for path in written:
            self.assertTrue(os.path.exists(path), path)

    def test_written_server_config_actually_imports(self):
        script = _Script(answers=[])
        init_config.apply(self._answers(), ROOT, script.write, script.read, force=True)
        path = os.path.join(self.tmp, "bigqmt_signal_trader_local_config.py")
        with io.open(path, encoding="utf-8") as handle:
            loaded = _load(handle.read(), path)

        self.assertEqual(loaded["BIGQMT_ACCOUNT_ID"], "8886800503")

    def test_existing_file_is_not_overwritten_without_consent(self):
        path = os.path.join(self.tmp, "bigqmt_signal_trader_local_config.py")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("# hand-tuned, do not clobber\n")

        script = _Script(answers=["n", "n"])  # decline both overwrites
        written = init_config.apply(
            self._answers(), ROOT, script.write, script.read, force=False)

        self.assertNotIn(path, written)
        with io.open(path, encoding="utf-8") as handle:
            self.assertIn("hand-tuned", handle.read())

    def test_consent_overwrites(self):
        path = os.path.join(self.tmp, "bigqmt_signal_trader_local_config.py")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("# stale\n")

        script = _Script(answers=["y", "y"])
        written = init_config.apply(
            self._answers(), ROOT, script.write, script.read, force=False)

        self.assertIn(path, written)
        with io.open(path, encoding="utf-8") as handle:
            self.assertNotIn("stale", handle.read())


class SingleFileInjectionTest(unittest.TestCase):
    """The generated build ships with placeholders; init has to replace them."""

    SAMPLE = (
        '#coding:gbk\n"""doc mentioning BIGQMT_ACCOUNT_ID / BIGQMT_REDIS_CONFIG"""\n'
        "import os\n\n\n"
        "# =========================== config block ===========================\n"
        'BIGQMT_ACCOUNT_ID = "YOUR_ACCOUNT_ID"\n\n'
        'BIGQMT_ACCOUNT_TYPE = "STOCK"\n\n'
        "BIGQMT_REDIS_CONFIG = {\n"
        '    "host": "127.0.0.1",\n'
        '    "rpc_allow_order_methods": False,\n'
        "}\n"
        "# ===================================================================\n"
        "print('rest of the build')\n"
    )

    def test_placeholders_are_replaced(self):
        result = init_config.inject_single_file_config(self.SAMPLE, _answers())

        self.assertNotIn("YOUR_ACCOUNT_ID", result)
        self.assertIn("'8886800503'", result)
        self.assertIn("'10.0.0.5'", result)

    def test_the_rest_of_the_build_is_untouched(self):
        result = init_config.inject_single_file_config(self.SAMPLE, _answers())

        self.assertIn("print('rest of the build')", result)
        self.assertIn("# ==========", result)

    def test_the_result_still_compiles(self):
        result = init_config.inject_single_file_config(self.SAMPLE, _answers())
        compile(result, "injected.py", "exec")

    def test_a_build_without_a_config_block_raises(self):
        """Returning the source unchanged would ship YOUR_ACCOUNT_ID as if it
        had worked."""
        with self.assertRaises(ValueError):
            init_config.inject_single_file_config("print('no config here')\n",
                                                  _answers())


if __name__ == "__main__":
    unittest.main()
