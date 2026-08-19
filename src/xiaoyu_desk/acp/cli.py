"""``xiaoyu-desk-acp`` — the agent backend KiroCrew spawns.

KiroCrew launches its agent through ``KIROCREW_KIRO_BIN``, a documented override
whose only requirement is that the named executable runs: there is no signature
check, no version floor, and no vendor check. It invokes the binary three ways,
all of which this entry point answers::

    <bin> --version                       # first-run readiness probe
    <bin> whoami                          # first-run readiness probe
    <bin> acp --agent NAME [--model ID]   # the ACP session itself
    <bin> chat --list-models --format json --no-interactive   # the model picker

One command is for people rather than KiroCrew:

    <bin> doctor [--agent NAME]   # check the setup before a session needs it

Unknown flags are tolerated rather than rejected. The argv is built by another
program on its own release schedule, and a new flag there must not turn every
session into a spawn failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .agentspec import AgentSpecError, agents_dir, load
from .factory import McpProvider, build_factory
from .proxy import DialectStdin, DialectStdout, KiroDialect

#: Printed by ``whoami``. KiroCrew's readiness probe only requires a zero exit
#: with output; the string names what is actually answering.
IDENTITY = "xiaoyu-desk (xiaoyu agent backend)"

#: The agent `doctor` checks when none is named. KiroCrew's managed default.
DEFAULT_AGENT = "kirocrew"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xiaoyu-desk-acp", add_help=False)
    parser.add_argument("command", nargs="?", default="")
    parser.add_argument("--agent", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--list-models", action="store_true")
    parser.add_argument(
        "--agents-dir",
        default="",
        help="Directory holding kiro agent specs. Defaults to $KIRO_HOME/agents "
        "(or ~/.kiro/agents), which is where KiroCrew writes them.",
    )
    return parser


def list_models() -> int:
    """Print xiaoyu's model catalog in the shape KiroCrew's picker reads.

    KiroCrew polls this every few seconds while the picker is degraded and
    reads the WHOLE of stdout as one JSON object, so nothing else may be
    printed there. A non-zero exit leaves the picker on its fallback, which is
    the right outcome when no provider is configured — better an honest
    fallback than a confidently empty list.

    Rows are the declared catalog, not an entitlement claim: KiroCrew narrows
    them against what a live session advertises before rendering.
    """
    # The wizard writes provider keys to the user-level .env, and this runs as a
    # bare one-shot with none of the session setup, so read it here or the
    # catalog comes back empty on a perfectly well-configured machine.
    from xiaoyu.config import Config, MissingConfig, context_window, load_dotenv, user_env_path

    load_dotenv(explicit=user_env_path())
    from xiaoyu import providers

    try:
        registry = providers.build(Config.from_env())
    except MissingConfig as exc:
        print(f"xiaoyu-desk-acp: no model provider configured ({exc})", file=sys.stderr)
        return 1

    models = [
        {
            "model_name": entry.model,
            "model_id": entry.model,
            "context_window_tokens": context_window(entry.model),
            "provider": entry.owner_label,
        }
        for entry in registry.listing()
    ]
    print(json.dumps({"models": models}, ensure_ascii=False))
    return 0


def serve(agent: str, model: str, directory: str) -> int:
    """Run one ACP connection over stdio until the client closes it."""
    try:
        spec = load(agent, agents_dir(directory or None))
    except AgentSpecError as exc:
        # Fail loudly and early: a session that starts without its prompt and
        # MCP servers looks healthy right up until the model needs one of them.
        print(f"xiaoyu-desk-acp: {exc}", file=sys.stderr)
        return 1

    # xiaoyu's sandbox wraps only the commands its bash tool runs, not its own
    # file writes, so KiroCrew's sandbox — which wraps this whole process tree —
    # is the layer that actually covers everything. On macOS the two cannot nest:
    # a seatbelt inside a seatbelt fails EPERM. setdefault, not assignment, so an
    # operator running KiroCrew with its sandbox off can export XIAOYU_SANDBOX=1
    # and get xiaoyu's own layer back.
    os.environ.setdefault("XIAOYU_SANDBOX", "0")

    # Deferred: importing AcpServer pulls in xiaoyu's provider stack, which the
    # --version and whoami probes have no use for and should not pay for.
    from xiaoyu import acp as xiaoyu_acp
    from xiaoyu.acp import AcpServer

    # Checked by behavior, not by version: the surface this adapter needs landed
    # without a version bump, so a version string names both builds. Missing it
    # would otherwise surface as an AttributeError from inside a session build,
    # which reads as a crash rather than as "upgrade xiaoyu-agent".
    missing = [
        name
        for name, present in (
            ("build_agent_factory", hasattr(xiaoyu_acp, "build_agent_factory")),
            ("AcpServer.agent_for", hasattr(AcpServer, "agent_for")),
        )
        if not present
    ]
    if missing:
        print(
            f"xiaoyu-desk-acp: the installed xiaoyu-agent lacks {', '.join(missing)}. "
            "Upgrade xiaoyu-agent to a build that exports the embedding surface.",
            file=sys.stderr,
        )
        return 1

    mcp = McpProvider(spec)
    dialect = KiroDialect(agent, sys.stdout)
    server = AcpServer(
        build_factory(spec, mcp, model=model),
        stdin=DialectStdin(dialect, sys.stdin),
        stdout=DialectStdout(dialect),
    )
    # Only now can the steer route reach the sessions: the server was built with
    # this dialect's own streams, so it could not have been passed in.
    dialect.bind(server)
    try:
        return server.serve() or 0
    finally:
        # The MCP servers are this process' children and nothing else reaps them;
        # xiaoyu's at-exit sweep only covers managers it built itself.
        mcp.close()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        print(f"xiaoyu-desk-acp {__version__}")
        return 0
    if args == ["whoami"]:
        print(IDENTITY)
        return 0

    parsed, _unknown = _parser().parse_known_args(args)
    if parsed.command == "doctor":
        from .doctor import run

        return run(parsed.agent or DEFAULT_AGENT, parsed.agents_dir)
    if parsed.command == "chat" and parsed.list_models:
        return list_models()
    if parsed.command != "acp":
        print(
            f"xiaoyu-desk-acp: unknown command {parsed.command!r} "
            "(expected 'acp', 'doctor', 'chat --list-models', '--version', or 'whoami')",
            file=sys.stderr,
        )
        return 2
    if not parsed.agent:
        print("xiaoyu-desk-acp: --agent is required", file=sys.stderr)
        return 2
    return serve(parsed.agent, parsed.model, parsed.agents_dir)


if __name__ == "__main__":
    raise SystemExit(main())
