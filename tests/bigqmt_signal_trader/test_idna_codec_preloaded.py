# coding: utf-8
"""Importing the redis transport must leave the idna codec ready to use.

First run of a strategy inside big QMT logged, once, on the first adjust
tick:

    LookupError: unknown encoding: idna

from socket.getaddrinfo inside the first Redis connect. The codec is lazy:
codecs.lookup('idna') -> encodings.search_function -> import encodings.idna
-> stringprep -> unicodedata (a C extension). That import ran on the adjust
thread -- a C++ timer callback -- and the sandboxed importer failed it there;
search_function swallows ImportError, so the caller saw an unknown encoding.
The next tick reconnected, the modules stayed in sys.modules, QMT keeps
sys.modules across re-runs, and the error never came back -- which is why it
looked like a one-off.

The fix imports encodings.idna and primes codecs.lookup('idna') at module
load, on the importing (main) thread, before any timer exists. Two things
follow, and both are pinned here:

* the three modules of the chain are in sys.modules, so a later __import__
  returns from there and never reaches a finder (the sandbox lives at the
  finder level -- cached modules bypass it);
* the interpreter's codec cache holds 'idna', so the adjust thread's lookup
  does not call search_function at all.

Only idna is preloaded. A non-ASCII host name would additionally need the
punycode codec; the bridge connects to ASCII hosts, whose labels take idna's
fast path and never reach punycode, and no such failure has been observed.

The codec cache is process-global and cannot be cleared from Python, so the
second property is checked in a fresh interpreter: import the transport,
evict the chain from sys.modules, install a finder that refuses to load it
(what the sandbox does), and the lookup must still answer from the cache.
"""

import importlib
import os
import subprocess
import sys
import textwrap
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

CHAIN = ("encodings.idna", "stringprep", "unicodedata")
TRANSPORT = "bigqmt_signal_trader.transports.redis_transport"


class IdnaPreloadedTest(unittest.TestCase):
    def test_importing_the_transport_caches_the_module_chain(self):
        for name in CHAIN:
            sys.modules.pop(name, None)
        sys.modules.pop(TRANSPORT, None)

        importlib.import_module(TRANSPORT)

        for name in CHAIN:
            self.assertIn(name, sys.modules,
                          "%s not loaded by importing the transport" % name)

    def test_the_lookup_answers_from_the_codec_cache_without_any_import(self):
        """Fresh interpreter: after importing the transport, evict the chain
        and refuse to load it at the finder level, the way the sandbox does
        on the adjust thread. codecs.lookup('idna') must still succeed."""
        probe = textwrap.dedent("""
            import codecs, importlib, sys
            sys.path.insert(0, %r)
            importlib.import_module(%r)

            for name in %r:
                sys.modules.pop(name, None)

            class Refuse(object):
                def find_spec(self, name, path=None, target=None):
                    if name.startswith("encodings.") or name in ("stringprep", "unicodedata"):
                        raise ImportError("sandbox refused " + name)
                    return None
            sys.meta_path.insert(0, Refuse())

            try:
                info = codecs.lookup("idna")
                # What getaddrinfo actually encodes: an ASCII host. Pure-ASCII
                # labels take idna's fast path and never touch punycode, so
                # this is the exact operation the adjust thread performs.
                hosts = [h.encode("idna").decode("ascii") for h in ("192.168.8.13", "redis.local")]
                print("OK", info.name, *hosts)
            except LookupError as exc:
                print("LOOKUPERROR", exc)
        """) % (SRC, TRANSPORT, CHAIN)

        out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                             text=True, encoding="utf-8", timeout=60)

        self.assertEqual(0, out.returncode, out.stderr)
        self.assertEqual("OK idna 192.168.8.13 redis.local", out.stdout.strip(),
                        "codec not primed at import; lookup fell through to the finder:\n%s%s"
                        % (out.stdout, out.stderr))

    def test_the_transport_import_survives_a_refused_idna(self):
        """The pre-import is guarded: a sandbox that refuses encodings.idna
        must not take the whole transport module down with it."""
        probe = textwrap.dedent("""
            import importlib, sys
            sys.path.insert(0, %r)

            class Refuse(object):
                def find_spec(self, name, path=None, target=None):
                    if name == "encodings.idna":
                        raise ImportError("sandbox refused " + name)
                    return None
            sys.meta_path.insert(0, Refuse())

            module = importlib.import_module(%r)
            print("OK" if hasattr(module, "RedisTransport") else "MISSING")
        """) % (SRC, TRANSPORT)

        out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                             text=True, encoding="utf-8", timeout=60)

        self.assertEqual(0, out.returncode, out.stderr)
        self.assertEqual("OK", out.stdout.strip(), out.stderr)


if __name__ == "__main__":
    unittest.main()
