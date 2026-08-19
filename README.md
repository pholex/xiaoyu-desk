# 羽案 · Xiaoyu Desk

Run [KiroCrew](https://github.com/kirodotdev/KiroCrew) on the
[xiaoyu](https://github.com/pholex/zhinu) agent instead of `kiro-cli`.

> **Closed beta.** Working and used daily, but read [Known
> limitations](#known-limitations) before you rely on it — a few KiroCrew
> features do not work through this adapter yet, and one of them fails silently.

KiroCrew drives its LLM through an ACP agent process and requires `kiro-cli` — a
closed-source binary that cannot be redistributed and that signs in to an Amazon
account. xiaoyu is an independent MIT-licensed coding agent that already speaks
ACP. This package is the adapter between them.

**You bring your own model.** Any of xiaoyu's providers — DeepSeek, Moonshot,
Qwen, Zhipu, Anthropic, OpenAI, xAI, or any OpenAI-compatible gateway. One API
key, no account to register.

**KiroCrew is not modified.** Not forked, not patched, not vendored here. The
adapter is a standalone executable that KiroCrew launches through
`KIROCREW_KIRO_BIN`, its own documented override — so you keep updating KiroCrew
normally, forever, with nothing to re-merge.

## Requirements

- Python 3.11+
- KiroCrew, installed and working
- An API key for one of xiaoyu's providers, or an OpenAI-compatible gateway

`kiro-cli` is **not** required. If it is installed, it is left alone.

## Install

```bash
# 1. the adapter
pip install xiaoyu-desk

# 2. point xiaoyu at your model (interactive wizard, writes a user-level .env)
xiaoyu config

# 3. run KiroCrew on it
KIROCREW_KIRO_BIN=$(which xiaoyu-desk-acp) kirocrew gateway
```

Make step 3 permanent by exporting `KIROCREW_KIRO_BIN` from your shell profile,
or by putting it in whatever launches your gateway.

To go back to `kiro-cli`, unset the variable. Nothing else changes.

## Check the setup

```bash
xiaoyu-desk-acp doctor
```

Every failure this adapter can have is silent — a session starts, looks healthy,
and behaves wrong. `doctor` runs those checks up front and names the one that is
broken: provider configured, agent spec readable, system prompt dereferenced, MCP
servers declared and actually injected, `KIROCREW_KIRO_BIN` pointing here, the
macOS sandbox delegation flag, and whether a gateway on a custom port will be
reachable by its own MCP servers.

It calls no model, spawns no MCP server, and needs no running gateway, so it is
safe to run at any time. Exit code is non-zero if anything failed, so it can gate
a script. **If you report a problem, please include its output.**

## Known limitations

**Read this before relying on it.** The first two are the ones people hit.

- **Mid-turn steer does nothing, and does not say so.** KiroCrew believes this
  backend implements steer. xiaoyu does have `Agent.steer()`, but its ACP server
  neither handles the method nor exposes the live session, so this proxy has
  nothing to route the request to and answers "not implemented" instead.
  Pressing steer produces no error and no effect. Use Stop and send a new message
  instead. This one is a missing access point upstream, not a missing capability.
- **Only the agent the gateway started with is available.** Per-session agent
  switching is refused, so alternate agents (`kirocrew-lite`, `-research`,
  `-heartbeat`, `-knowledge`) cannot be selected from the session picker. The
  refusal is explicit — the session fails with a message rather than silently
  running the wrong agent.
- **xiaoyu's own interaction modes are unreachable.** KiroCrew reads the ACP
  `modes` list as an agent selector, so the adapter has to overwrite it with the
  single spawned agent — which leaves xiaoyu's `plan` and `auto` modes with
  nowhere to be advertised. Every KiroCrew session therefore runs in xiaoyu's
  `default` mode, confirming writes and commands one by one. Tool approval itself
  works normally; it is only the mode *switch* that has no channel.
- **Compaction status, agent-switched notices, and the TODO panel stay empty.**
  Those are `kiro-cli`-specific notifications that xiaoyu never emits. Nothing
  breaks; the panels just have nothing to show.
- **The "not signed in" hint is generic.** With no provider configured you get a
  generic error rather than "run `xiaoyu config`".

Verified working: streaming chat, tool approval (allow and reject), Stop,
background subagents with parent/subagent concurrency, MCP tools, the model
picker and switching, and conversation continuity across an agent restart.

Not yet exercised: cron jobs, Slack/Discord channels, long-conversation
compaction, artifacts, knowledge, task runner, apps. Tested on macOS only.

## Running a second instance on a non-default port

If you start a gateway with `--port`, **also set `dashboard.url` in that data
home's `config.json`**:

```json
{"dashboard": {"url": "http://localhost:8899"}}
```

KiroCrew's MCP servers resolve the gateway they call back into from
`dashboard.url` alone — nothing tells them which port the gateway actually bound.
Left empty they dial the default port, which on a machine already running
KiroCrew is *another instance's* gateway. Internal calls are then rejected with a
bare `Forbidden`, and only the calls that need it fail: reads go through,
`spawn_run` and friends do not. Nothing in the message points at the port.

This is how KiroCrew's MCP bridge resolves its own gateway, not something the
adapter introduces — but you meet it the first time you run a second instance
alongside the app, which is exactly what evaluating this invites.

## Sandboxing

xiaoyu's own sandbox wraps only the commands its bash tool runs — not its own
file writes. KiroCrew's sandbox wraps this entire process tree, so it is the
layer that actually covers everything, and on macOS the two cannot nest (a
seatbelt inside a seatbelt fails `EPERM`).

The adapter therefore sets `XIAOYU_SANDBOX=0` with `setdefault`. **If you run
KiroCrew with its own sandbox disabled, export `XIAOYU_SANDBOX=1`** to get
xiaoyu's layer back; an explicit value is always respected.

On macOS, also confirm `~/.kiro/settings/amazon-internal.json` either does not
exist or does not set `sandbox` to true. KiroCrew skips its own seatbelt for what
it believes is `kiro-cli`'s internal sandbox when that flag is on — and this
adapter has no such internal sandbox. A missing file reads as false, so a machine
without `kiro-cli` installed is already correct.

---

## How it works

KiroCrew talks to `kiro-cli`, whose ACP surface froze around a draft of the
protocol. Two of the calls it makes — `session/set_model` and the
`models: {availableModels, currentModelId}` response field — were never
stabilized and were **removed from ACP on 2026-06-01**, with model selection
moving to Session Config Options. xiaoyu implements the current standard. The two
cannot talk without a translator, and that translator is the whole job here.

The adapter injects in two places, both ordinary constructor arguments of
xiaoyu's `AcpServer`. Nothing in xiaoyu was changed to accommodate this, and
nothing in KiroCrew was either.

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
That has a real cost — xiaoyu's own `plan` and `auto` modes lose the only field
they could have been advertised in, so no KiroCrew session can switch modes.
Publishing them as a Session Config Option (the way `model` already is) would
give them a channel kiro has not claimed; that is a xiaoyu-side change.

A `session/set_mode` naming the spawned agent is acknowledged; naming any other
one is **refused, not faked**. Acknowledging a switch that did not happen would leave the
session on the spawned agent while KiroCrew believed it had moved to another —
silently widening what the model may do whenever the requested agent is narrower.

### 2. The session factory (`factory.py`, `agentspec.py`)

KiroCrew writes its agent definitions to `<kiro home>/agents/<name>.json` and
names one with `--agent` at spawn. That file — not the ACP wire — is where a
session's MCP servers and system prompt live: KiroCrew's shared MCP gateway is
opt-in and off by default, so on a normal install nothing arrives through
`session/new`. An adapter that ignores that file hands the model a coding agent
with none of KiroCrew's capabilities.

So the adapter reads it and injects:

- `prompt` → **dereferenced, then** appended to xiaoyu's system prompt. KiroCrew
  writes a `file://` URL here, not the prose; taken literally the model's entire
  system prompt becomes a URL and nothing errors — the session looks healthy
  while the agent's instructions never arrive.
- `mcpServers` → `ServerSpec` records handed to an adapter-owned `McpManager`,
  which **replaces** xiaoyu's own config discovery rather than merging with it.
  The agent spec is the single source of truth for what the session may reach;
  the operator's personal `mcp.json` does not leak in.
- `allowedTools` → **deliberately not translated.** Its entries are kiro tool
  names that do not name xiaoyu tools, so any mapping would be a guess, and a
  wrong guess pre-approves what the operator never approved. Every tool call
  travels the ACP approval bridge to KiroCrew's own prompt instead.

The MCP servers are loaded, and their "server connected" announcement settled,
**before** the first prompt. xiaoyu delivers that announcement through
`Agent.notify`, and a notification landing while the model is writing prose is
re-delivered at the step boundary and forces another step — the model answers the
same question twice and the client concatenates both replies. It shows up as a
doubled first answer (`OK` rendering as `OKOK`) and nowhere else.

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

## Releasing

Publishing runs on GitHub Actions with PyPI Trusted Publishing — no API token
exists anywhere, on a laptop or in a secret.

1. Bump `__version__` in `src/xiaoyu_desk/__init__.py` (the only place it lives;
   `pyproject.toml` reads it dynamically).
2. Commit, then `git tag vX.Y.Z && git push origin vX.Y.Z`.

The tag runs CI first and publishes only if it is green, after checking that the
tag matches `__version__`. A tag can never ship a red build: by the time anyone
noticed, the artifact would already be on PyPI with its version number burned
permanently.

## License

MIT. KiroCrew itself is Apache-2.0 and is neither included nor modified here.
Kiro and Kiro Crew are trademarks of their respective owner; this project is not
affiliated with or endorsed by them.
