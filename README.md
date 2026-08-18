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

**KiroCrew's own environment is forwarded to those servers.** xiaoyu builds a
stdio server's environment from a whitelist instead of inheriting one — a sound
default for arbitrary third-party servers, and wrong for these: `kirocrew-core`
and friends *are* KiroCrew, and without `KIROCREW_HOME` they resolve the
**default** data home rather than this session's. On a machine running a second
instance that means they read state from, and dial the gateway of, the wrong
one — silently, because reads succeed against a real instance and only calls
needing a session identity are refused. So the adapter forwards `KIROCREW_*` and
`KIRO_HOME` into each server's env, restoring the footing kiro-cli's servers get
by plain inheritance. A value declared in the agent spec always wins over the
forwarded one, and nothing forwarded is a credential — the gateway scrubs channel
tokens from this process's environment before it is spawned.

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

## Running on a non-default port

If you start the gateway with `--port`, **also set `dashboard.url` in the data
home's `config.json`**:

```json
{"dashboard": {"url": "http://localhost:8899"}}
```

KiroCrew's MCP servers resolve the gateway they call back into from
`dashboard.url` alone — nothing tells them which port the gateway actually bound.
Left empty they dial the default port, which on a machine already running
KiroCrew is *another instance's* gateway. Every internal call is then rejected
with a bare `Forbidden`, and only the calls that need it fail: reads go through,
`spawn_run` and friends do not. Nothing in the message points at the port.

This is not specific to this adapter — it is how KiroCrew's MCP bridge resolves
its own gateway — but you will meet it the first time you run a second instance
alongside the app, which is exactly what testing this adapter invites.

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

The `xiaoyu-agent` dependency is pinned exactly, not ranged: the adapter reaches
past xiaoyu's CLI into its library surface (`AcpServer`, `Toolbox`,
`McpManager`), which a release is free to reshape.

`0.33.0` is the floor for a working adapter rather than a preference — it is the
first release where `Toolbox(mcp_view=...)` is honored for a full toolbox. Before
it, the injection was accepted and silently dropped: the session came up healthy
with none of the agent spec's MCP servers and the operator's own `mcp.json` in
their place. `factory.py` asserts that behavior at session build rather than
trusting the pin, because the fix once existed unreleased under an
already-published version number and no version specifier could tell the two
apart. That assertion stays as a sentinel against future drift.

## License

MIT. KiroCrew itself is Apache-2.0 and is neither included nor modified here.
