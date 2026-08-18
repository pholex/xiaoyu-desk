# Xiaoyu Desk

Run [KiroCrew](https://github.com/kirodotdev/KiroCrew) on the
[xiaoyu](https://github.com/pholex/zhinu) agent instead of `kiro-cli`.

KiroCrew drives its LLM through an ACP agent process and requires `kiro-cli` — a
closed-source binary that cannot be redistributed and that signs in to an Amazon
account. xiaoyu is an independent MIT-licensed coding agent that already speaks
ACP. This package is the adapter between them.

**KiroCrew is not modified.** It is not forked, patched, or vendored here. The
adapter is a standalone executable that KiroCrew launches through
`KIROCREW_KIRO_BIN`, its own documented override — so upstream KiroCrew can be
updated with a plain `git pull` or a reinstall, forever, with nothing to re-merge.

## Install

```bash
pip install -e .
```

## Use

```bash
KIROCREW_KIRO_BIN=$(which xiaoyu-desk-acp) kirocrew gateway
```

xiaoyu needs its own provider configured (`xiaoyu config`) — an API key or an
OpenAI-compatible gateway. There is no account to sign in to.

## How it works

KiroCrew talks to `kiro-cli`, whose ACP surface froze around a draft of the
protocol. Two of the calls it makes — `session/set_model` and the
`models: {availableModels, currentModelId}` response field — were never
stabilized and were **removed from ACP on 2026-06-01**, with model selection
moving to Session Config Options. xiaoyu implements the current standard. The two
cannot talk without a translator, and that translator is the whole job here.

The adapter injects in two places, both of which are ordinary constructor
arguments of xiaoyu's `AcpServer`. Nothing in xiaoyu was changed to accommodate
this, and nothing in KiroCrew was either.

### 1. The wire (`proxy.py`)

A line-level proxy around stdin/stdout:

| KiroCrew sends | xiaoyu sees |
|---|---|
| `session/set_model` | `session/set_config_option` (configId `model`); the reply is rewritten back to `{}` |
| `_kiro.dev/*`, `_session/steer` | nothing — answered `-32601` by the proxy |
| `session/set_mode` | nothing — answered by the proxy, see below |

| xiaoyu replies | KiroCrew sees |
|---|---|
| `configOptions` | plus a `models` block derived from it |
| `modes` (xiaoyu's three interaction modes) | `modes` naming the spawned agent |

That last rewrite is load-bearing rather than cosmetic. KiroCrew reads the ACP
`modes` list as an **agent selector**, and it fails closed when the list omits the
agent it asked for — tearing the session down rather than risk running a broader
agent than requested. xiaoyu advertising its own interaction modes trips that
guard on every session.

The proxy advertises exactly one mode: the agent this process was spawned with.
A `session/set_mode` naming that agent is acknowledged; naming any other one is
**refused, not faked**. Acknowledging a switch that did not happen would leave the
session on the spawned agent while KiroCrew believed it had moved to another —
silently widening what the model may do whenever the requested agent is narrower.
Per-session agent switching is simply not supported yet, and it says so.

### 2. The session factory (`factory.py`, `agentspec.py`)

KiroCrew writes its agent definitions to `<kiro home>/agents/<name>.json` and
names one with `--agent` at spawn. That file — not the ACP wire — is where a
session's MCP servers and system prompt live: KiroCrew's shared MCP gateway is
opt-in and off by default, so on a normal install nothing arrives through
`session/new`. An adapter that ignores that file hands the model a coding agent
with none of KiroCrew's capabilities.

So the adapter reads it and injects:

- `prompt` → appended to xiaoyu's system prompt.
- `mcpServers` → `ServerSpec` records handed to an adapter-owned `McpManager`,
  which **replaces** xiaoyu's own config discovery rather than merging with it.
  The agent spec is the single source of truth for what the session may reach;
  the operator's personal `mcp.json` does not leak in.
- `allowedTools` → **deliberately not translated.** Its entries are kiro tool
  names that do not name xiaoyu tools, so any mapping would be a guess, and a
  wrong guess pre-approves what the operator never approved. Every tool call
  travels the ACP approval bridge to KiroCrew's own prompt instead.

`${VAR}` placeholders in server env are passed through unexpanded, so an
unresolvable one fails in the server that needs it rather than quietly becoming
an empty string.

## Sandboxing

xiaoyu's own sandbox wraps only the commands its bash tool runs — not its own
file writes. KiroCrew's sandbox wraps this entire process tree, so it is the
layer that actually covers everything, and on macOS the two cannot nest (a
seatbelt inside a seatbelt fails `EPERM`).

The adapter therefore sets `XIAOYU_SANDBOX=0` with `setdefault`. **If you run
KiroCrew with its own sandbox disabled, export `XIAOYU_SANDBOX=1`** to get
xiaoyu's layer back; the explicit value is respected.

On macOS, also confirm `~/.kiro/settings/amazon-internal.json` either does not
exist or does not set `sandbox` to true. KiroCrew skips its own seatbelt for what
it believes is `kiro-cli`'s internal sandbox when that flag is on — and this
adapter has no such internal sandbox. A missing file reads as false, so a machine
without `kiro-cli` installed is already correct.

## Known gaps

- **Mid-turn steer does not work.** KiroCrew believes this backend implements
  `_session/steer`; xiaoyu does not, so the request is answered `-32601` and
  dropped. Pressing steer produces no error and no effect.
- **Per-session agent switching is refused** (see above).
- **`_kiro.dev/compaction/status`, `agent/switched`, and the TODO panel stay
  empty.** They are kiro-cli-specific notifications that xiaoyu never emits.
- **The "not signed in" hint is generic.** xiaoyu answers `-32000 auth_required`
  when no provider is configured, while KiroCrew detects auth failures by a
  stderr pattern, so the message is less specific than it could be.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests
```

The `xiaoyu-agent` dependency is pinned to a **commit**, not a version. The
adapter reaches past xiaoyu's CLI into its library surface (`AcpServer`,
`Toolbox`, `McpManager`), which a release is free to reshape — but the immediate
reason is sharper: `Toolbox(mcp_view=...)` is only honored for a full toolbox as
of the pinned commit, and that change landed without a version bump. `0.32.0`
therefore names two different behaviors, and the one published to PyPI is the one
where this adapter's MCP injection is silently dropped. No version specifier can
distinguish them.

`factory.py` asserts the behavior at session build for the same reason, so an
unsuitable build fails loudly instead of yielding a session with no capabilities.
The pin becomes `xiaoyu-agent==0.33.0` once that release ships.

## License

MIT. KiroCrew itself is Apache-2.0 and is neither included nor modified here.
