"""Agent-spec translation tests: kiro JSON in, xiaoyu inputs out."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu_desk.acp.agentspec import AgentSpecError, agents_dir, forwarded_env, load


def _write(directory: Path, name: str, data: dict) -> None:
    (directory / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


class TestAgentsDir(unittest.TestCase):
    def test_kiro_home_override_is_honored(self):
        # KiroCrew resolves the agents directory through KIRO_HOME. Resolving it
        # any other way reads a directory it never wrote to, and the symptom is
        # silent: the session simply comes up with no MCP servers.
        previous = os.environ.get("KIRO_HOME")
        os.environ["KIRO_HOME"] = "/somewhere/else"
        try:
            self.assertEqual(agents_dir(), Path("/somewhere/else/agents"))
        finally:
            if previous is None:
                os.environ.pop("KIRO_HOME", None)
            else:
                os.environ["KIRO_HOME"] = previous

    def test_explicit_directory_wins_over_the_environment(self):
        self.assertEqual(agents_dir("/opt/specs"), Path("/opt/specs"))


class TestLoad(unittest.TestCase):
    @mock.patch.dict(os.environ, {}, clear=True)
    def test_prompt_and_servers_are_translated(self):
        # Cleared env so the exact-equality assertion below stays true wherever
        # this runs: under a real gateway the process DOES carry KIROCREW_*, and
        # forwarding it is the point of test_kirocrew_env_is_forwarded_to_servers.
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, "kirocrew", {
                "name": "kirocrew",
                "prompt": "You are the crew.",
                "mcpServers": {
                    "kirocrew-core": {
                        "command": "kirocrew",
                        "args": ["mcp", "core"],
                        "env": {"KIROCREW_HOME": "/data"},
                    }
                },
            })
            spec = load("kirocrew", directory)
        self.assertEqual(spec.prompt, "You are the crew.")
        self.assertEqual(len(spec.servers), 1)
        server = spec.servers[0]
        self.assertEqual(server.name, "kirocrew-core")
        self.assertEqual(server.command, "kirocrew")
        self.assertEqual(server.args, ["mcp", "core"])
        self.assertEqual(server.env, {"KIROCREW_HOME": "/data"})

    def test_placeholders_are_passed_through_unexpanded(self):
        # Constructing ServerSpec directly bypasses xiaoyu's ${VAR} expansion,
        # which is wanted: an unresolvable placeholder should fail in the server
        # that needs the value, not quietly become an empty string here.
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, "a", {
                "mcpServers": {"s": {"command": "x", "env": {"TOKEN": "${SECRET_TOKEN}"}}}
            })
            spec = load("a", directory)
        self.assertEqual(spec.servers[0].env["TOKEN"], "${SECRET_TOKEN}")

    def test_allowed_tools_are_not_translated(self):
        # kiro tool names do not name xiaoyu tools, so any mapping is a guess —
        # and a wrong guess pre-approves what the operator never approved. Every
        # call travels the approval bridge instead.
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, "a", {"allowedTools": ["fs_read", "execute_bash"], "prompt": "p"})
            spec = load("a", directory)
        self.assertEqual(spec.prompt, "p")
        self.assertEqual(spec.servers, [])

    def test_non_stdio_server_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, "a", {
                "mcpServers": {
                    "remote": {"url": "https://example.invalid/mcp"},
                    "local": {"command": "run-me"},
                }
            })
            spec = load("a", directory)
        self.assertEqual([s.name for s in spec.servers], ["local"])
        # …and the skip is recorded rather than dropped on the floor: a server
        # that vanishes without a word is a tool the model silently lacks.
        self.assertEqual([name for name, _reason in spec.skipped], ["remote"])
        self.assertIn("stdio", spec.skipped[0][1])

    def test_a_malformed_entry_is_recorded_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, "a", {"mcpServers": {"bad": ["not", "an", "object"]}})
            spec = load("a", directory)
        self.assertEqual(spec.servers, [])
        self.assertEqual([name for name, _reason in spec.skipped], ["bad"])

    def test_a_fully_usable_spec_skips_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, "a", {"mcpServers": {"s": {"command": "x"}}})
            spec = load("a", directory)
        self.assertEqual(spec.skipped, [])

    def test_kirocrew_env_is_forwarded_to_servers(self):
        # xiaoyu builds a stdio server's env from a whitelist rather than
        # inheriting it. KiroCrew's own servers need KIROCREW_HOME or they bind
        # to the DEFAULT data home — on a machine running a second instance that
        # means they read and dial the WRONG one, silently.
        with mock.patch.dict(os.environ, {"KIROCREW_HOME": "/data/home", "KIRO_HOME": "/k"}):
            with tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                _write(directory, "a", {"mcpServers": {"s": {"command": "x"}}})
                spec = load("a", directory)
        self.assertEqual(spec.servers[0].env["KIROCREW_HOME"], "/data/home")
        self.assertEqual(spec.servers[0].env["KIRO_HOME"], "/k")

    def test_spec_env_wins_over_forwarded_env(self):
        # The file is the operator's explicit statement about this server.
        with mock.patch.dict(os.environ, {"KIROCREW_HOME": "/from/env"}):
            with tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                _write(directory, "a", {
                    "mcpServers": {"s": {"command": "x", "env": {"KIROCREW_HOME": "/from/spec"}}}
                })
                spec = load("a", directory)
        self.assertEqual(spec.servers[0].env["KIROCREW_HOME"], "/from/spec")

    def test_forwarded_env_carries_no_unrelated_variables(self):
        with mock.patch.dict(os.environ, {"KIROCREW_HOME": "/h", "AWS_SECRET_ACCESS_KEY": "x"}):
            forwarded = forwarded_env()
        self.assertIn("KIROCREW_HOME", forwarded)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", forwarded)

    def test_file_url_prompt_is_dereferenced(self):
        # KiroCrew writes a file:// URL here, not the prose. Taken literally the
        # model's entire system prompt becomes a URL — and nothing errors, so the
        # session looks healthy while the agent's instructions never arrive.
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            prompt_file = directory / "prompt.md"
            prompt_file.write_text("You are the crew.\n", encoding="utf-8")
            _write(directory, "a", {"prompt": prompt_file.as_uri()})
            spec = load("a", directory)
        self.assertEqual(spec.prompt, "You are the crew.")

    def test_inline_prompt_is_still_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, "a", {"prompt": "inline instructions"})
            spec = load("a", directory)
        self.assertEqual(spec.prompt, "inline instructions")

    def test_unreadable_prompt_file_degrades_to_no_prompt(self):
        # Better xiaoyu's own system prompt than one that is the string "file://…".
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, "a", {"prompt": "file:///nonexistent/prompt.md"})
            spec = load("a", directory)
        self.assertEqual(spec.prompt, "")

    def test_missing_spec_raises_rather_than_starting_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AgentSpecError):
                load("absent", Path(tmp))

    def test_malformed_spec_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "a.json").write_text("[]", encoding="utf-8")
            with self.assertRaises(AgentSpecError):
                load("a", directory)


if __name__ == "__main__":
    unittest.main()
