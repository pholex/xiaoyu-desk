"""Read a kiro-format agent spec and translate it into xiaoyu inputs.

KiroCrew materializes its agent definitions as JSON under ``<kiro home>/agents/``
and names one with ``--agent <name>`` at spawn. That file — not the ACP wire — is
where a session's MCP servers and system prompt actually live: KiroCrew's shared
MCP gateway is opt-in (``mcp_gateway.enabled`` defaults to false), so on a default
install nothing is injected through ``session/new`` and this file is the only
source. An adapter that ignores it hands the model a coding agent with none of
KiroCrew's capabilities.

Only two of the spec's fields cross into xiaoyu:

``prompt``
    Appended to xiaoyu's own system prompt. It carries the agent's persona and
    operating instructions, which have no ACP wire field of their own.

``mcpServers``
    Translated to ``ServerSpec`` and handed to a caller-owned ``McpManager``.
    ``${VAR}`` placeholders are passed through unexpanded — constructing a
    ``ServerSpec`` directly bypasses ``load_server_specs``' expansion, which is
    the wanted behavior: an unresolvable placeholder should fail in the server
    that needs it, not silently become an empty string here.

``allowedTools`` is deliberately NOT translated. Its entries are kiro tool names
(``fs_read``, ``execute_bash``) that do not correspond to xiaoyu's tools, so any
mapping would be a guess — and a wrong guess pre-approves something the operator
never approved. Leaving it out costs nothing real: every tool call then travels
the ACP approval bridge to KiroCrew's own prompt, which is the path that already
honors the operator's approval settings.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from xiaoyu.mcp import ServerSpec

#: Fallback kiro home. ``KIRO_HOME`` is kiro-cli's own documented override and
#: KiroCrew honors it when resolving the agents directory, so the adapter must
#: resolve it the same way or it reads a directory KiroCrew never wrote to.
DEFAULT_KIRO_HOME = "~/.kiro"

#: Environment the agent spec's MCP servers must inherit from this process.
#:
#: xiaoyu builds a stdio server's environment from a whitelist rather than
#: inheriting — a sound default for arbitrary third-party servers. But KiroCrew's
#: own servers ARE KiroCrew: without ``KIROCREW_HOME`` they resolve the DEFAULT
#: data home instead of this session's, and then read another instance's state,
#: dial another instance's gateway, and refuse anything that needs a session
#: identity they can no longer resolve. kiro-cli's servers inherit its
#: environment wholesale, so this restores the same footing.
#:
#: Nothing here is a credential. The gateway scrubs channel tokens from this
#: process's environment before spawning it, so what remains is exactly the set
#: KiroCrew intends its agent tree to carry.
_FORWARDED_ENV_PREFIXES = ("KIROCREW_",)
_FORWARDED_ENV_KEYS = ("KIRO_HOME",)


def forwarded_env() -> dict[str, str]:
    """KiroCrew-owned environment this process must pass to its MCP servers."""
    return {
        key: value
        for key, value in os.environ.items()
        if key.startswith(_FORWARDED_ENV_PREFIXES) or key in _FORWARDED_ENV_KEYS
    }


class AgentSpecError(Exception):
    """The named agent spec is missing or unreadable."""


@dataclass
class AgentSpec:
    """The parts of a kiro agent JSON that reach xiaoyu."""

    name: str
    prompt: str = ""
    servers: list[ServerSpec] = field(default_factory=list)


def agents_dir(explicit: str | None = None) -> Path:
    """Resolve the directory holding kiro agent specs.

    Mirrors KiroCrew's ``kiro_agents_dir()`` = ``kiro_home() / "agents"``, where
    ``kiro_home()`` honors ``KIRO_HOME``. Resolving it any other way would read a
    different directory than the one KiroCrew writes, and the failure is silent:
    the agent simply comes up with no MCP servers.
    """
    if explicit:
        return Path(explicit).expanduser()
    home = os.environ.get("KIRO_HOME") or DEFAULT_KIRO_HOME
    return Path(home).expanduser() / "agents"


def _servers_from(raw: object) -> list[ServerSpec]:
    """Translate a kiro ``mcpServers`` map into xiaoyu ``ServerSpec`` records.

    Shape-tolerant on purpose: the file is written by another program, so an
    entry that is not a usable stdio server declaration is skipped rather than
    raising. A malformed entry must not cost the session every other server.
    """
    if not isinstance(raw, dict):
        return []
    specs: list[ServerSpec] = []
    for name, entry in raw.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        command = entry.get("command")
        if not isinstance(command, str) or not command:
            # No command means a transport this adapter does not speak (an HTTP
            # or SSE server). Skipping keeps the stdio ones working.
            continue
        args = [str(a) for a in entry.get("args", []) if isinstance(a, (str, int, float))]
        env_raw = entry.get("env")
        declared = (
            {str(k): str(v) for k, v in env_raw.items()} if isinstance(env_raw, dict) else {}
        )
        # Forwarded first so the spec's own declarations win: the file is the
        # operator's explicit statement about this server and must not be
        # overridden by what happens to be in this process's environment.
        specs.append(
            ServerSpec(
                name=name,
                command=command,
                args=args,
                env={**forwarded_env(), **declared},
                disabled=bool(entry.get("disabled", False)),
            )
        )
    return specs


def load(agent: str, directory: Path | None = None) -> AgentSpec:
    """Load the spec named *agent* from *directory*.

    Raises ``AgentSpecError`` when the file is missing or is not a JSON object.
    Failing loudly here is deliberate: a session that silently starts with no
    prompt and no MCP servers looks like a working session until the model is
    asked to do something it no longer can.
    """
    directory = directory if directory is not None else agents_dir()
    path = directory / f"{agent}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AgentSpecError(f"agent spec not found: {path}") from exc
    except (OSError, ValueError) as exc:
        raise AgentSpecError(f"agent spec unreadable ({path}): {exc}") from exc
    if not isinstance(data, dict):
        raise AgentSpecError(f"agent spec is not a JSON object: {path}")
    prompt = data.get("prompt")
    return AgentSpec(
        name=agent,
        prompt=prompt if isinstance(prompt, str) else "",
        servers=_servers_from(data.get("mcpServers")),
    )
