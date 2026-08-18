"""Agent-spec translation tests: kiro JSON in, xiaoyu inputs out."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from xiaoyu_desk.acp.agentspec import AgentSpecError, agents_dir, load


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
    def test_prompt_and_servers_are_translated(self):
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
