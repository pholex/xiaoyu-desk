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

#: How long to wait for the agent spec's MCP servers before starting a session.
#: Generous because a cold schema cache means real subprocess startup, and the
#: cost of giving up early is a doubled first answer (see McpProvider.view).
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
        """Return this session's view, starting the servers on first use.

        Always returns a view, even when the spec declares no servers: an empty
        view means "this agent has no MCP servers", whereas passing None to
        ``Toolbox`` would fall back to config discovery and silently hand the
        session the operator's personal ``mcp.json`` — servers the agent spec
        never granted it.

        Blocks until the servers are ready, which is what makes the FIRST answer
        of a session correct rather than doubled. xiaoyu announces a server
        coming online through ``Agent.notify``, and a notification that lands
        while the model is producing prose is re-delivered at the step boundary
        and **forces another step** — so the model answers the same question
        twice and the client concatenates both into one reply. Loading the
        servers before the first prompt keeps that announcement out of a turn.
        kiro-cli behaves the same way (its servers load during ``session/new``),
        and KiroCrew already tolerates the wait: it tracks MCP init progress for
        exactly this phase.
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


def _assert_view_honored(toolbox: Toolbox, view: McpView) -> None:
    """Fail loudly if the installed xiaoyu ignored the injected MCP view.

    Older ``Toolbox`` builds accept ``mcp_view`` but only consult it for the
    restricted-subset path, silently falling back to config discovery for a full
    toolbox. Nothing raises: the session starts, reports healthy, and simply has
    none of the agent spec's servers — while the operator's personal ``mcp.json``
    takes their place. That is both a capability loss and a scope leak, and it is
    invisible until a model says a tool does not exist.

    A version check would not catch it (the fix shipped without a version bump,
    so one version string names two different behaviors), so this asserts the
    behavior itself through the public accessor.

    As of the pinned 0.33.0 that precedence is stated in xiaoyu's own source and
    covered by an upstream contract test, so this is a regression net rather than
    a guard against a live hazard. It stays because the pin is the only thing
    keeping it that way, and a pin is one edit away from moving.
    """
    if toolbox.mcp_manager is not view:
        raise RuntimeError(
            "the installed xiaoyu-agent ignores Toolbox(mcp_view=...) for a full "
            "toolbox, so this session would silently run without the agent spec's "
            "MCP servers and with the operator's own mcp.json instead. Upgrade "
            "xiaoyu-agent to a build where an explicit mcp_view takes precedence "
            "over config discovery."
        )


def _settle_mcp_announcement(toolbox: Toolbox) -> None:
    """Absorb the MCP roster once BEFORE the agent can be notified about it.

    xiaoyu announces "MCP server X connected" through ``Agent.notify`` the first
    time a toolbox assembles its schemas. That first assembly otherwise happens
    inside the session's FIRST turn, and a notification that arrives while the
    model is producing prose is re-delivered at the step boundary and **forces
    another step** — so the model answers the same question a second time and
    the client renders both, concatenated. It looks exactly like a duplicated
    reply ("OK" becoming "OKOK") and only on a session's first answer.

    The announcement is bookkept per (server, tool-set fingerprint), so it fires
    once and then only on a real change. Assembling here with a hook that
    discards the message records that bookkeeping, and the in-turn assembly then
    finds nothing changed. A no-op hook rather than none: ``_announce_mcp``
    returns early while ``notify_hook`` is None and skips the bookkeeping with
    it, so leaving the hook unset settles nothing. ``Agent.__init__`` installs
    the real hook afterwards, so only this first announcement is swallowed.

    The model still reaches every MCP tool through ``search_tool`` — only the
    unsolicited "server connected" nudge is dropped, and the agent spec's own
    system prompt already describes the tooling.
    """
    toolbox.notify_hook = lambda *_args, **_kwargs: None
    toolbox.schemas()


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

        view = mcp.view()
        toolbox = Toolbox(config, mcp_view=view)
        _assert_view_honored(toolbox, view)
        _settle_mcp_announcement(toolbox)

        agent = Agent(
            config,
            toolbox,
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
