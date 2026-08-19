"""Doctor tests: each check must report the silent failure it exists to catch."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu_desk.acp import doctor
from xiaoyu_desk.acp.agentspec import AgentSpec


def _write(directory: Path, name: str, data: dict) -> None:
    (directory / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


class TestPrompt(unittest.TestCase):
    def test_a_file_url_that_never_dereferenced_is_a_failure(self):
        # The exact bug: the model's whole system prompt becomes a URL, and
        # nothing anywhere reports a problem.
        r = doctor._prompt(AgentSpec(name="a", prompt="file:///x/prompt.md"))
        self.assertEqual(r.status, doctor.FAIL)

    def test_real_prose_passes(self):
        self.assertEqual(doctor._prompt(AgentSpec(name="a", prompt="You are…")).status, doctor.OK)

    def test_no_prompt_warns_rather_than_fails(self):
        # Workable — the session just runs on xiaoyu's own prompt.
        self.assertEqual(doctor._prompt(AgentSpec(name="a", prompt="")).status, doctor.WARN)


class TestAgentSpec(unittest.TestCase):
    def test_missing_spec_fails_and_lists_what_is_there(self):
        # "not found" alone sends people hunting; naming the specs that DO exist
        # usually shows the typo immediately.
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, "kirocrew-lite", {})
            result, spec = doctor._agent_spec("kirocrew", str(directory))
        self.assertEqual(result.status, doctor.FAIL)
        self.assertIn("kirocrew-lite", result.detail)
        self.assertIsNone(spec)

    def test_present_spec_passes_and_is_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, "kirocrew", {"prompt": "hi"})
            result, spec = doctor._agent_spec("kirocrew", str(directory))
        self.assertEqual(result.status, doctor.OK)
        self.assertEqual(spec.prompt, "hi")


class TestMcpInjection(unittest.TestCase):
    def test_passes_against_the_installed_xiaoyu(self):
        # The pin claims a build that honors mcp_view; this observes it, because
        # the broken and fixed builds once shared a version number.
        self.assertEqual(doctor._mcp_injection().status, doctor.OK)


class TestGatewayPort(unittest.TestCase):
    def _home(self, tmp: str, url: str, ports: list[str]) -> None:
        root = Path(tmp)
        (root / "config.json").write_text(json.dumps({"dashboard": {"url": url}}))
        (root / "run").mkdir(exist_ok=True)
        for p in ports:
            (root / "run" / f"gateway-{p}.pid").write_text("1")

    def test_empty_url_on_the_default_port_is_fine(self):
        # The common case. Warning here would cry wolf on every normal install.
        with tempfile.TemporaryDirectory() as tmp:
            self._home(tmp, "", [doctor._DEFAULT_DASHBOARD_PORT])
            with mock.patch.dict(os.environ, {"KIROCREW_HOME": tmp}):
                self.assertEqual(doctor._gateway_port().status, doctor.OK)

    def test_empty_url_on_a_custom_port_warns(self):
        # This is the trap: the gateway bound 8899, but its MCP servers dial the
        # default port — another instance's gateway if one is running there.
        with tempfile.TemporaryDirectory() as tmp:
            self._home(tmp, "", ["8899"])
            with mock.patch.dict(os.environ, {"KIROCREW_HOME": tmp}):
                r = doctor._gateway_port()
        self.assertEqual(r.status, doctor.WARN)
        self.assertIn("8899", r.detail)

    def test_matching_url_and_port_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._home(tmp, "http://localhost:8899", ["8899"])
            with mock.patch.dict(os.environ, {"KIROCREW_HOME": tmp}):
                self.assertEqual(doctor._gateway_port().status, doctor.OK)

    def test_no_running_gateway_is_not_a_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._home(tmp, "", [])
            with mock.patch.dict(os.environ, {"KIROCREW_HOME": tmp}):
                self.assertEqual(doctor._gateway_port().status, doctor.OK)


class TestReport(unittest.TestCase):
    def test_exit_code_is_nonzero_when_something_failed(self):
        # So it can gate a script, not just be read.
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            code = doctor.run("absent-agent", str(tmp), out=out)
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out.getvalue())

    def test_every_check_is_reported_even_when_an_early_one_fails(self):
        # A doctor that stops at the first problem hides the others, which is
        # the opposite of the point.
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            doctor.run("absent-agent", str(tmp), out=out)
        text = out.getvalue()
        for title in ("model provider", "MCP injection", "KIROCREW_KIRO_BIN", "gateway port"):
            self.assertIn(title, text)


if __name__ == "__main__":
    unittest.main()
