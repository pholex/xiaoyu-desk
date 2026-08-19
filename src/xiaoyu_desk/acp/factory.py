"""Build xiaoyu sessions that carry a kiro agent spec's prompt and MCP servers.

``AcpServer`` takes the factory that mints one ``Agent`` per ACP session, which
is the second place this adapter injects (the first is the wire; see
``proxy.py``). Everything that has no ACP wire field of its own — the system
prompt, the MCP server roster — enters here rather than being smuggled through
the protocol.

The session assembly itself is xiaoyu's, not ours: ``build_agent_factory`` is
the same chain the xiaoyu CLI runs, exported for embedding hosts. This module
used to carry a line-by-line fork of it, which drifted — the fork silently
lacked ``install_exit_logging``, so exit reasons never reached the session log.
What is left here is only what is genuinely this adapter's: the agent spec's
prompt and MCP servers going in.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from xiaoyu.agent import Agent
from xiaoyu.mcp import McpManager, McpView

from .agentspec import AgentSpec

#: How long to wait for the agent spec's MCP servers before starting a session.
#: Generous because a cold schema cache means real subprocess startup, and the
#: cost of giving up early is a first turn without the agent's tools.
READY_TIMEOUT_SECS = 30.0


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

    def __init__(self, spec: AgentSpec, ready_timeout: float = READY_TIMEOUT_SECS) -> None:
        self._spec = spec
        self._ready_timeout = ready_timeout
        self._lock = threading.Lock()
        self._manager: McpManager | None = None

    def view(self) -> McpView:
        """Return the view, starting the servers on first use.

        Always returns a view, even when the spec declares no servers: an empty
        view means "this agent has no MCP servers", whereas passing None to the
        factory would fall back to config discovery and silently hand the session
        the operator's personal ``mcp.json`` — servers the agent spec never
        granted it.

        Blocks until the servers are ready, so the session's FIRST turn already
        has the agent spec's tools. A model that asks for a tool during the one
        turn the roster was still loading is told it does not exist, and it
        plans around the absence for the rest of the conversation.

        kiro-cli behaves the same way (its servers load during ``session/new``),
        and KiroCrew already tolerates the wait: it tracks MCP init progress for
        exactly this phase.

        This used to carry a second reason — a server announcement landing
        mid-prose forced an extra step and the model answered twice. xiaoyu
        0.34.0 delivers that announcement without waking a step, so only the
        capability reason above is left.
        """
        with self._lock:
            if self._manager is None:
                manager = McpManager(self._spec.servers)
                manager.start()
                # Bounded, not indefinite: a server that never becomes ready
                # must cost a doubled first answer, not a session that never
                # starts. wait_ready returns as soon as every server has a
                # verdict, ready or failed.
                manager.wait_ready(self._ready_timeout)
                self._manager = manager
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
    default. A resumed session's recorded model wins over both, which xiaoyu's
    factory already handles — the alternative silently moves an existing
    conversation onto a different model.

    The MCP view is resolved HERE rather than per session, because the factory
    takes it once at construction. So the servers start when the connection is
    served rather than when the first session is created — the same point
    kiro-cli starts its own.
    """
    from xiaoyu.acp import build_agent_factory

    view = mcp.view()
    inner = build_agent_factory(
        model=model or None,
        append_system_prompt=spec.prompt or None,
        mcp_view=view,
    )

    def build_agent(*args: Any, **kwargs: Any) -> tuple[Agent, list[dict[str, Any]]]:
        agent, history = inner(*args, **kwargs)
        if agent.toolbox.mcp_manager is not view:
            # The agent spec's servers were dropped and config discovery ran in
            # their place, which is both a capability loss and a scope leak: the
            # operator's personal mcp.json reaching a session that was never
            # granted it. Nothing else reports this — the session starts clean.
            raise RuntimeError(
                "the installed xiaoyu-agent ignored the injected mcp_view, so this "
                "session would run without the agent spec's MCP servers and with "
                "the operator's own mcp.json instead"
            )
        return agent, history

    return build_agent
