"""Build xiaoyu sessions that carry a kiro agent spec's prompt and MCP servers.

``AcpServer`` takes the factory that mints one ``Agent`` per ACP session, which
is the second place this adapter injects (the first is the wire; see
``proxy.py``). Everything that has no ACP wire field of its own — the system
prompt, the MCP server roster — enters here rather than being smuggled through
the protocol.

Modeled on xiaoyu's own ACP factory, with two departures:

* the agent spec's ``prompt`` is appended to the system prompt, and
* MCP servers come from an adapter-owned manager instead of config discovery.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from xiaoyu import folder_trust
from xiaoyu.agent import Agent
from xiaoyu.config import Config
from xiaoyu.mcp import McpManager, McpView
from xiaoyu.permissions import Permissions
from xiaoyu.session_log import (
    SessionLog,
    check_session_id,
    find_named,
    last_mode,
    last_model,
    open_named,
)
from xiaoyu.tools import Toolbox

from .agentspec import AgentSpec


class McpProvider:
    """Process-wide owner of the MCP servers declared by the agent spec.

    One manager serves every session, mirroring kiro-cli — which starts its
    servers once per process rather than once per session. Per-session managers
    would multiply every server process by the session count.

    **This class owns processes and nothing else will reap them.** A manager
    built here is not registered in xiaoyu's process-level manager cache, so the
    at-exit sweep that covers ``mcp.launch`` managers does not cover this one.
    ``close`` must run on the way out; see ``cli.serve``.
    """

    def __init__(self, spec: AgentSpec) -> None:
        self._spec = spec
        self._lock = threading.Lock()
        self._manager: McpManager | None = None

    def view(self) -> McpView:
        """Return this session's view, starting the servers on first use.

        Always returns a view, even when the spec declares no servers: an empty
        view means "this agent has no MCP servers", whereas passing None to
        ``Toolbox`` would fall back to config discovery and silently hand the
        session the operator's personal ``mcp.json`` — servers the agent spec
        never granted it.
        """
        with self._lock:
            if self._manager is None:
                self._manager = McpManager(self._spec.servers)
                # Lazy by contract: with a warm schema cache this registers the
                # servers without spawning anything until a tool is really called.
                self._manager.start()
            return McpView(self._manager, "all")

    def close(self) -> None:
        with self._lock:
            if self._manager is not None:
                self._manager.close()
                self._manager = None


def build_factory(
    spec: AgentSpec,
    mcp: McpProvider,
    *,
    model: str = "",
) -> Callable[..., tuple[Agent, list[dict[str, Any]]]]:
    """Return the ``agent_factory`` for ``AcpServer``.

    ``model`` is the id KiroCrew pinned at spawn; empty means xiaoyu's own
    default. A resumed session's recorded model wins over both, matching xiaoyu's
    behavior — the alternative silently moves an existing conversation onto a
    different model.
    """

    def build_agent(
        workspace: Path,
        approver: Any,
        sink: Any,
        session_name: str,
        create: bool,
    ) -> tuple[Agent, list[dict[str, Any]]]:
        trusted = folder_trust.evaluate(workspace, interactive=False).verdict == "trusted"
        overrides: dict[str, Any] = {"workspace_trusted": trusted}
        if model:
            overrides["model"] = model
        if spec.prompt:
            overrides["append_system_prompt"] = spec.prompt
        config = Config.from_env(workspace=workspace, **overrides)
        permissions = Permissions.load(config.workspace, include_workspace=trusted)

        history: list[dict[str, Any]] = []
        follow_mode = ""
        if create:
            session_log = SessionLog.create(
                config.model, str(config.workspace), session_id=session_name
            )
        else:
            # A load must find the real session file. "Open or create" semantics
            # would answer an unknown sessionId with an empty conversation that
            # looks resumed, so the id is validated and located first.
            try:
                check_session_id(session_name)
            except ValueError as exc:
                raise LookupError(f"invalid sessionId {session_name!r}: {exc}") from exc
            path = find_named(session_name, str(config.workspace))
            if path is None:
                raise LookupError(f"unknown sessionId {session_name!r}")
            if recorded := last_model(path):
                config.model = recorded
            follow_mode = last_mode(path)
            session_log, history = open_named(
                session_name, config.model, str(config.workspace)
            )

        agent = Agent(
            config,
            Toolbox(config, mcp_view=mcp.view()),
            approver=approver,
            session_log=session_log,
            permissions=permissions,
            sink=sink,
        )
        if history:
            # copy=False: continue the existing transcript rather than writing a
            # second copy of every message already on disk.
            agent.restore(history, copy=False)
        if follow_mode:
            agent.adopt_mode(follow_mode)
        return agent, history

    return build_agent
