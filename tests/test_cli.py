"""CLI-surface tests: the three shapes KiroCrew invokes the binary with."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from xiaoyu_desk.acp import cli


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestProbes(unittest.TestCase):
    """KiroCrew's first-run readiness gate needs a zero exit and some output."""

    def test_version(self):
        code, out, _ = _run(["--version"])
        self.assertEqual(code, 0)
        self.assertTrue(out.strip())

    def test_whoami(self):
        code, out, _ = _run(["whoami"])
        self.assertEqual(code, 0)
        self.assertTrue(out.strip())

    def test_unknown_command_is_refused(self):
        code, _, err = _run(["nonsense"])
        self.assertEqual(code, 2)
        self.assertIn("nonsense", err)

    def test_acp_requires_an_agent(self):
        code, _, err = _run(["acp"])
        self.assertEqual(code, 2)
        self.assertIn("--agent", err)


class TestListModels(unittest.TestCase):
    """`chat --list-models` feeds the dashboard's model picker.

    Missing it does not fail loudly — KiroCrew logs a 503 and quietly renders a
    fallback list, which is how it went unnoticed until the gateway log was read.
    """

    def test_emits_the_shape_the_picker_parses(self):
        listing = [
            mock.Mock(model="deepseek-v4-pro", owner_label="direct deepseek"),
            mock.Mock(model="kimi-k3", owner_label="direct moonshot"),
        ]
        registry = mock.Mock(**{"listing.return_value": listing})
        with mock.patch("xiaoyu.providers.build", return_value=registry):
            code, out, _ = _run(["chat", "--list-models", "--format", "json", "--no-interactive"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual([m["model_name"] for m in payload["models"]],
                         ["deepseek-v4-pro", "kimi-k3"])
        for row in payload["models"]:
            self.assertEqual(row["model_id"], row["model_name"])
            self.assertIsInstance(row["context_window_tokens"], int)
            self.assertGreater(row["context_window_tokens"], 0)

    def test_stdout_carries_json_and_nothing_else(self):
        # The whole of stdout is parsed as one JSON object, so a stray print
        # anywhere on this path breaks the picker rather than degrading it.
        registry = mock.Mock(**{"listing.return_value": []})
        with mock.patch("xiaoyu.providers.build", return_value=registry):
            _, out, _ = _run(["chat", "--list-models"])
        json.loads(out)  # must not raise

    def test_unconfigured_provider_exits_nonzero_instead_of_empty(self):
        # An empty catalog would read as "this account has no models". A non-zero
        # exit leaves KiroCrew on its fallback list, which is the honest answer.
        from xiaoyu.config import MissingConfig

        with mock.patch("xiaoyu.providers.build", side_effect=MissingConfig("no key")):
            code, out, err = _run(["chat", "--list-models"])
        self.assertNotEqual(code, 0)
        self.assertEqual(out.strip(), "")
        self.assertIn("provider", err)


if __name__ == "__main__":
    unittest.main()
