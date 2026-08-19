"""``xiaoyu-desk-acp doctor`` — check the setup before a session depends on it.

Every check here exists because the corresponding failure is **silent**. None of
them raise, log, or show up in the dashboard; each one produces a session that
looks healthy and behaves wrong:

* no provider configured → the first turn fails with a generic error
* agent spec missing → ``session/new`` fails, and the message names ACP internals
* prompt left as a ``file://`` URL → the model runs on none of its instructions
* an ``mcpServers`` entry the adapter cannot translate → it is dropped at parse
  time and the model quietly lacks those tools (rarer since xiaoyu speaks
  Streamable HTTP: only old-style SSE and malformed entries fall here)
* an xiaoyu build that drops ``mcp_view`` → the session silently gets the
  operator's personal ``mcp.json`` instead of the agent spec's servers
* ``dashboard.url`` unset on a non-default port → MCP servers dial *another*
  instance's gateway and read its state

So this runs them all up front and says which one is wrong, which is the
question a beta tester cannot otherwise answer.

Deliberately side-effect free: no model call, no MCP server spawned, no gateway
required. It can be run any time, including while a gateway is live.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

OK = "ok"
WARN = "warn"
FAIL = "FAIL"

#: Where KiroCrew keeps the flag that makes it skip its own sandbox for what it
#: believes is kiro-cli's internal one. This adapter has no internal sandbox.
_KIRO_INTERNAL_SETTINGS = "~/.kiro/settings/amazon-internal.json"

#: The port KiroCrew's MCP clients fall back to when ``dashboard.url`` is empty.
#: Only used to avoid crying wolf on a default install, where the gateway binds
#: exactly this port and the empty setting is therefore correct. If it ever
#: drifts the cost is one spurious warning, never a wrong instruction.
_DEFAULT_DASHBOARD_PORT = "5476"


@dataclass
class Result:
    status: str
    title: str
    detail: str = ""


def _provider() -> Result:
    """Can xiaoyu reach a model at all?"""
    try:
        from xiaoyu import providers
        from xiaoyu.config import Config, MissingConfig, load_dotenv, user_env_path

        load_dotenv(explicit=user_env_path())
        try:
            registry = providers.build(Config.from_env())
        except MissingConfig:
            return Result(
                FAIL,
                "model provider",
                "none configured — run `xiaoyu config`",
            )
        names = [entry.model for entry in registry.listing()]
        return Result(OK, "model provider", f"{len(names)} models, e.g. {', '.join(names[:3])}")
    except Exception as exc:  # pragma: no cover - defensive
        return Result(FAIL, "model provider", f"could not check: {exc}")


def _agent_spec(agent: str, directory: str) -> tuple[Result, object]:
    """Is the agent spec KiroCrew will name actually readable?"""
    from .agentspec import AgentSpecError, agents_dir, load

    resolved = agents_dir(directory or None)
    try:
        spec = load(agent, resolved)
    except AgentSpecError as exc:
        available = ""
        try:
            found = sorted(p.stem for p in resolved.glob("*.json"))
            available = f"; specs present: {', '.join(found) or 'none'}"
        except OSError:
            pass
        return Result(FAIL, f"agent spec {agent!r}", f"{exc}{available}"), None
    return Result(OK, f"agent spec {agent!r}", f"read from {resolved}"), spec


def _prompt(spec: object) -> Result:
    """Did the system prompt dereference, or is the model getting a URL?"""
    prompt = getattr(spec, "prompt", "")
    if not prompt:
        return Result(
            WARN,
            "system prompt",
            "empty — the agent runs on xiaoyu's own prompt with none of KiroCrew's",
        )
    if prompt.startswith("file://"):
        return Result(FAIL, "system prompt", "still a file:// URL; it did not dereference")
    return Result(OK, "system prompt", f"{len(prompt)} characters")


def _mcp_servers(spec: object) -> Result:
    """Which of the agent spec's servers actually reached xiaoyu?

    An entry this adapter cannot translate is dropped during parsing, so the
    roster simply comes out shorter with nothing to explain the gap. That is the
    silent failure: the model is missing tools the agent spec granted it, and the
    first sign is a model saying a tool does not exist. So a skip is reported
    here, by name and with its reason, even when other servers translated fine.
    """
    servers = list(getattr(spec, "servers", []))
    skipped = list(getattr(spec, "skipped", []))
    if skipped:
        dropped = "; ".join(f"{name} ({reason})" for name, reason in skipped)
        kept = (
            f"{len(servers)} usable ({', '.join(s.name for s in servers)}), "
            if servers
            else "none usable, "
        )
        return Result(WARN, "MCP servers", f"{kept}{len(skipped)} skipped: {dropped}")
    if not servers:
        return Result(
            WARN,
            "MCP servers",
            "the agent spec declares none — the model gets no KiroCrew tools",
        )
    return Result(OK, "MCP servers", f"{len(servers)}: {', '.join(s.name for s in servers)}")


def _mcp_injection() -> Result:
    """Does the INSTALLED xiaoyu honor an injected mcp_view for a full toolbox?

    The failure this catches shipped in a released version under a version
    number that also names the fixed build, so no dependency specifier can
    express it — the behavior has to be observed.
    """
    try:
        import tempfile

        from xiaoyu.config import Config
        from xiaoyu.mcp import McpManager, McpView
        from xiaoyu.tools import Toolbox

        with tempfile.TemporaryDirectory() as tmp:
            config = Config.from_env(workspace=Path(tmp), workspace_trusted=False)
            # Empty spec list: this constructs no server and spawns no process.
            view = McpView(McpManager([]), "all")
            toolbox = Toolbox(config, mcp_view=view)
        if toolbox.mcp_manager is not view:
            return Result(
                FAIL,
                "MCP injection",
                "installed xiaoyu ignores Toolbox(mcp_view=...); sessions would run "
                "on your personal mcp.json instead of the agent spec's servers",
            )
        import xiaoyu

        return Result(OK, "MCP injection", f"honored by xiaoyu {xiaoyu.__version__}")
    except Exception as exc:  # pragma: no cover - defensive
        return Result(FAIL, "MCP injection", f"could not check: {exc}")


def _kiro_bin() -> Result:
    """Is KiroCrew pointed at this adapter?"""
    configured = os.environ.get("KIROCREW_KIRO_BIN", "")
    if not configured:
        return Result(
            WARN,
            "KIROCREW_KIRO_BIN",
            "unset here — set it where you launch the gateway, or KiroCrew uses kiro-cli",
        )
    try:
        same = Path(configured).resolve() == Path(sys.argv[0]).resolve()
    except OSError:
        same = False
    if not same:
        return Result(WARN, "KIROCREW_KIRO_BIN", f"points at {configured}, not this binary")
    return Result(OK, "KIROCREW_KIRO_BIN", configured)


def _sandbox_delegation() -> Result:
    """On macOS, would KiroCrew skip its seatbelt for a sandbox we do not have?"""
    if sys.platform != "darwin":
        return Result(OK, "sandbox delegation", "not applicable off macOS")
    path = Path(_KIRO_INTERNAL_SETTINGS).expanduser()
    try:
        enabled = bool(json.loads(path.read_text(encoding="utf-8")).get("sandbox", False))
    except (OSError, ValueError):
        return Result(OK, "sandbox delegation", "kiro internal sandbox flag absent — correct")
    if enabled:
        return Result(
            WARN,
            "sandbox delegation",
            f"{path} sets sandbox=true, so KiroCrew skips its own seatbelt for what it "
            "thinks is kiro-cli's internal one — this adapter has none, leaving the "
            "agent unconfined",
        )
    return Result(OK, "sandbox delegation", "kiro internal sandbox off — correct")


def _gateway_port() -> Result:
    """Will the MCP servers dial the gateway that actually started?"""
    home = os.environ.get("KIROCREW_HOME") or "~/.kiro/crew"
    root = Path(home).expanduser()
    try:
        url = (json.loads((root / "config.json").read_text(encoding="utf-8"))
               .get("dashboard", {}).get("url", ""))
    except (OSError, ValueError):
        return Result(OK, "gateway port", "no KiroCrew config here — nothing to compare")
    try:
        bound = sorted(p.stem.split("-")[-1] for p in (root / "run").glob("gateway-*.pid"))
    except OSError:
        bound = []
    if not bound:
        return Result(OK, "gateway port", "no gateway running from this data home")
    if not url:
        if _DEFAULT_DASHBOARD_PORT in bound:
            return Result(OK, "gateway port", f"default port {_DEFAULT_DASHBOARD_PORT}")
        return Result(
            WARN,
            "gateway port",
            f"a gateway is on port {', '.join(bound)} but dashboard.url is empty, so its "
            f"MCP servers dial the default {_DEFAULT_DASHBOARD_PORT} — another instance's "
            "gateway if one runs there. Set dashboard.url to this gateway's own port.",
        )
    if not any(port in url for port in bound):
        return Result(
            WARN,
            "gateway port",
            f"dashboard.url is {url} but the running gateway is on {', '.join(bound)}",
        )
    return Result(OK, "gateway port", url)


def run(agent: str, directory: str, out=None) -> int:
    """Run every check, print a report, return a shell exit code."""
    stream = out if out is not None else sys.stdout
    spec_result, spec = _agent_spec(agent, directory)
    results = [_provider(), spec_result]
    if spec is not None:
        results += [_prompt(spec), _mcp_servers(spec)]
    results += [_mcp_injection(), _kiro_bin(), _sandbox_delegation(), _gateway_port()]

    width = max(len(r.title) for r in results)
    for r in results:
        line = f"{r.status:<4} {r.title.ljust(width)}"
        print(f"{line}  {r.detail}" if r.detail else line, file=stream)

    failures = sum(1 for r in results if r.status == FAIL)
    warnings = sum(1 for r in results if r.status == WARN)
    print(file=stream)
    if failures:
        print(f"{failures} failure(s), {warnings} warning(s).", file=stream)
        return 1
    print(f"No failures, {warnings} warning(s).", file=stream)
    return 0
