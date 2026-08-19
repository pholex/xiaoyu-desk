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
from xiaoyu_desk.acp.factory import McpProvider, build_factory


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

    def test_factory_refuses_a_session_that_fell_back_to_discovery(self):
        # Models a xiaoyu build that accepts mcp_view and ignores it. The session
        # would come up healthy carrying the operator's personal mcp.json instead
        # of the agent spec's servers — a capability loss and a scope leak that
        # nothing else reports.
        provider = McpProvider(AgentSpec(name="a", servers=[]))

        class _WrongToolbox:
            mcp_manager = "whatever config discovery produced"
            notify_hook = None

            def schemas(self):
                return []

        agent = mock.Mock(toolbox=_WrongToolbox())
        with mock.patch(
            "xiaoyu.acp.build_agent_factory",
            return_value=lambda *a, **k: (agent, []),
        ):
            try:
                factory = build_factory(AgentSpec(name="a"), provider)
                with self.assertRaises(RuntimeError) as caught:
                    factory(Path("/tmp"), None, None, "sess-1", True)
            finally:
                provider.close()
        self.assertIn("mcp_view", str(caught.exception))


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
