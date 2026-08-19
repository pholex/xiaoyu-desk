"""Session-factory tests, centered on the MCP injection actually taking effect."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu.config import Config
from xiaoyu.mcp import McpManager, McpView, ServerSpec
from xiaoyu.tools import Toolbox

from xiaoyu_desk.acp.agentspec import AgentSpec
from xiaoyu_desk.acp.factory import (
    McpProvider,
    _assert_view_honored,
    _settle_mcp_announcement,
)


def _config() -> Config:
    with tempfile.TemporaryDirectory() as tmp:
        return Config.from_env(workspace=Path(tmp), workspace_trusted=False)


class TestViewIsHonored(unittest.TestCase):
    """The injection must reach the toolbox, not merely be handed to it.

    This is the failure that end-to-end testing caught and a naive unit test did
    not: asserting on the VIEW proves the view works, while the toolbox quietly
    used config discovery instead. Assert on what the toolbox ended up with.
    """

    def test_full_toolbox_uses_the_injected_view(self):
        # No specs => no server processes, so this stays a pure wiring check.
        view = McpView(McpManager([]), "all")
        toolbox = Toolbox(_config(), mcp_view=view)
        self.assertIs(toolbox.mcp_manager, view)

    def test_guard_accepts_a_toolbox_that_honored_the_view(self):
        view = McpView(McpManager([]), "all")
        toolbox = Toolbox(_config(), mcp_view=view)
        _assert_view_honored(toolbox, view)  # must not raise

    def test_guard_rejects_a_toolbox_that_fell_back_to_discovery(self):
        # Models the older xiaoyu build: mcp_view accepted, then ignored for a
        # full toolbox. The session would come up healthy with the wrong servers.
        view = McpView(McpManager([]), "all")

        class _IgnoringToolbox:
            mcp_manager = "whatever config discovery produced"

        with self.assertRaises(RuntimeError) as caught:
            _assert_view_honored(_IgnoringToolbox(), view)
        self.assertIn("mcp_view", str(caught.exception))


class TestMcpAnnouncementSettling(unittest.TestCase):
    """The first schema assembly must not be able to notify the agent.

    xiaoyu announces a connected MCP server through Agent.notify, and a
    notification arriving while the model is writing prose forces an extra step
    — the model answers twice and the client concatenates both ("OK" -> "OKOK").
    Settling the announcement before the Agent exists is what prevents that.
    """

    def test_schemas_is_assembled_with_a_hook_installed(self):
        # The subtlety that made a first attempt fail: _announce_mcp returns
        # early when notify_hook is None and skips its own bookkeeping with it,
        # so assembling with no hook settles nothing and the announcement fires
        # again inside the turn.
        seen: list[object] = []

        class _Toolbox:
            notify_hook = None

            def schemas(_self):
                seen.append(_self.notify_hook)
                return []

        box = _Toolbox()
        _settle_mcp_announcement(box)
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0], "schemas() ran with notify_hook=None; nothing was settled")

    def test_the_installed_hook_discards_the_message(self):
        box = mock.Mock(**{"schemas.return_value": []})
        _settle_mcp_announcement(box)
        # Must accept the (text, key) shape xiaoyu calls it with, and swallow it.
        self.assertIsNone(box.notify_hook("MCP server X connected", "mcp-online-X-abc"))


class TestMcpProvider(unittest.TestCase):
    def test_a_spec_with_no_servers_still_yields_a_view(self):
        # Returning None here would let Toolbox fall back to config discovery and
        # hand the session the operator's personal mcp.json — servers the agent
        # spec never granted it. "No servers" must mean no servers.
        provider = McpProvider(AgentSpec(name="a", servers=[]))
        try:
            view = provider.view()
            self.assertIsInstance(view, McpView)
            self.assertEqual(view.ready_tools(), [])
        finally:
            provider.close()

    def test_sessions_share_one_manager(self):
        # Per-session managers would multiply every server process by the session
        # count; kiro-cli starts its servers once per process. Asserted by
        # counting constructions rather than by reading the manager back off a
        # view: McpView's contract is that it holds no process and its lifetime
        # belongs to the manager, so it deliberately offers no reverse lookup.
        provider = McpProvider(AgentSpec(name="a", servers=[]))
        with mock.patch(
            "xiaoyu_desk.acp.factory.McpManager", wraps=McpManager
        ) as constructed:
            try:
                first, second = provider.view(), provider.view()
            finally:
                provider.close()
        self.assertIsNot(first, second)
        self.assertEqual(constructed.call_count, 1)

    def test_close_is_idempotent(self):
        # Nothing else reaps these processes, so close runs on every exit path
        # and may well run twice.
        provider = McpProvider(AgentSpec(name="a", servers=[ServerSpec(name="s", command="true")]))
        provider.close()
        provider.close()


if __name__ == "__main__":
    unittest.main()
