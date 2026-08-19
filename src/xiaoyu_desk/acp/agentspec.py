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
    Translated to ``ServerSpec`` and handed to a caller-owned ``McpManager``,
    stdio and Streamable HTTP alike. ``${VAR}`` placeholders are passed through
    unexpanded — constructing a ``ServerSpec`` directly bypasses
    ``load_server_specs``' expansion, which is the wanted behavior: an
    unresolvable placeholder should fail in the server that needs it, not
    silently become an empty string here.

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
from urllib.parse import urlparse
from urllib.request import url2pathname

from xiaoyu.mcp import ServerSpec

#: Fallback kiro home. ``KIRO_HOME`` is kiro-cli's own documented override and
#: KiroCrew honors it when resolving the agents directory, so the adapter must
#: resolve it the same way or it reads a directory KiroCrew never wrote to.
DEFAULT_KIRO_HOME = "~/.kiro"

#: Cap on a prompt file. Generous for prose; refuses a pathological file.
_MAX_PROMPT_BYTES = 1024 * 1024

#: Environment the agent spec's MCP servers must inherit from this process,
#: named the way ``ServerSpec.inherit_env`` wants it (exact names and
#: ``PREFIX_*`` patterns).
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
#:
#: Declared rather than assembled: xiaoyu resolves these at spawn, and its own
#: precedence (whitelist < inherit_env < the spec's declared env) already puts
#: the agent spec's word last, which is what this adapter wants.
INHERIT_ENV = ["KIROCREW_*", "KIRO_HOME"]


class AgentSpecError(Exception):
    """The named agent spec is missing or unreadable."""


@dataclass
class AgentSpec:
    """The parts of a kiro agent JSON that reach xiaoyu."""

    name: str
    prompt: str = ""
    servers: list[ServerSpec] = field(default_factory=list)
    #: ``mcpServers`` entries this adapter could not translate, as
    #: ``(name, reason)``. Carried rather than discarded so ``doctor`` can name
    #: them: a dropped entry is a tool the model silently does not have, and the
    #: roster alone gives no hint that anything went missing.
    skipped: list[tuple[str, str]] = field(default_factory=list)


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


def _servers_from(raw: object) -> tuple[list[ServerSpec], list[tuple[str, str]]]:
    """Translate a kiro ``mcpServers`` map into xiaoyu ``ServerSpec`` records.

    Shape-tolerant on purpose: the file is written by another program, so an
    entry that is not a usable stdio server declaration is skipped rather than
    raising. A malformed entry must not cost the session every other server.

    Returns the translated servers AND what was skipped, with a reason for each.
    Skipping silently was itself one of the silent failures this project exists
    to remove: the session comes up with a shorter roster, the model finds a tool
    missing, and nothing anywhere says which entry was dropped or why.
    """
    if not isinstance(raw, dict):
        return [], []
    specs: list[ServerSpec] = []
    skipped: list[tuple[str, str]] = []
    for name, entry in raw.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            skipped.append((str(name), "not a named JSON object"))
            continue
        command = entry.get("command")
        url = entry.get("url")
        if (not isinstance(command, str) or not command) and isinstance(url, str) and url:
            # Streamable HTTP. xiaoyu runs it through the same admission gates,
            # circuit breaker and generation bookkeeping as a stdio server; only
            # the transport differs, so nothing else here changes.
            #
            # `type` is not consulted: an entry carrying a url is an HTTP server
            # by construction. Old-style SSE declares itself the same way and is
            # NOT supported (xiaoyu advertises `sse: false`), so it arrives here
            # looking identical and is accepted — then fails in the server with
            # its own message rather than being guessed at from a field kiro
            # does not reliably write.
            headers_raw = entry.get("headers")
            specs.append(
                ServerSpec(
                    name=name,
                    command="",
                    url=url,
                    headers=(
                        {str(k): str(v) for k, v in headers_raw.items()}
                        if isinstance(headers_raw, dict)
                        else {}
                    ),
                    disabled=bool(entry.get("disabled", False)),
                )
            )
            continue
        if not isinstance(command, str) or not command:
            skipped.append((name, "neither a command nor a url — no transport to speak"))
            continue
        args = [str(a) for a in entry.get("args", []) if isinstance(a, (str, int, float))]
        env_raw = entry.get("env")
        declared = (
            {str(k): str(v) for k, v in env_raw.items()} if isinstance(env_raw, dict) else {}
        )
        specs.append(
            ServerSpec(
                name=name,
                command=command,
                args=args,
                env=declared,
                inherit_env=list(INHERIT_ENV),
                disabled=bool(entry.get("disabled", False)),
            )
        )
    return specs, skipped


def _resolve_prompt(raw: object) -> str:
    """Return the system prompt named by an agent spec's ``prompt`` field.

    KiroCrew writes a ``file://`` URL there rather than the prose — its shipped
    agents point at ``kiro_crew/config/prompt.md``. Taken literally the model
    receives a URL as its entire system prompt, which is not an error anywhere:
    the session starts, the model just never sees its instructions.

    An inline string is still honored, since a hand-written spec may carry one.
    Unreadable file, or one larger than the cap, degrades to no prompt rather
    than raising — a session with xiaoyu's own system prompt is far better than
    no session, and far better than one whose prompt is the string "file://…".
    """
    if not isinstance(raw, str) or not raw.strip():
        return ""
    text = raw.strip()
    if text.startswith("file://"):
        path = Path(url2pathname(urlparse(text).path))
    elif text.startswith("/") and text.endswith(".md"):
        # A bare path is not a documented shape, but it is the obvious next
        # spelling and costs one branch to honor.
        path = Path(text)
    else:
        return text
    try:
        if path.stat().st_size > _MAX_PROMPT_BYTES:
            return ""
        return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError, UnicodeDecodeError):
        return ""


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
    servers, skipped = _servers_from(data.get("mcpServers"))
    return AgentSpec(
        name=agent,
        prompt=_resolve_prompt(data.get("prompt")),
        servers=servers,
        skipped=skipped,
    )
