"""Translate between KiroCrew's ACP dialect and xiaoyu's standards-track one.

KiroCrew drives kiro-cli, whose ACP surface froze around an unstable draft:
``session/set_model`` and the ``models: {availableModels, currentModelId}``
response field were never stabilized and were removed from the protocol on
2026-06-01, with model selection moving to Session Config Options. xiaoyu
implements the current standard, so the two cannot talk without a translator.

The translation runs as a line-level proxy wrapped around ``AcpServer``'s stdin
and stdout, which that class already accepts as constructor arguments. Nothing
in xiaoyu needs to change, and the dialect stays quarantined in this file.

What crosses the boundary:

============================  ==================================================
KiroCrew sends                what xiaoyu sees
============================  ==================================================
``session/set_model``         ``session/set_config_option`` (configId ``model``),
                              and the reply is rewritten back to ``{}``
``_kiro.dev/*``               nothing — answered ``-32601`` here
``_session/steer``            nothing — answered ``-32601`` here
``session/set_mode``          nothing — answered here; see ``_on_set_mode``
============================  ==================================================

============================  ==================================================
xiaoyu replies                what KiroCrew sees
============================  ==================================================
``configOptions``             plus a ``models`` block derived from it
``modes`` (xiaoyu's three     ``modes`` naming the spawned agent, because
interaction modes)            KiroCrew reads this field as an agent selector
============================  ==================================================

The ``modes`` rewrite is not cosmetic. KiroCrew fails CLOSED when a session
advertises modes that do not include the agent it asked for: it tears the session
down rather than risk running a broader agent than requested. xiaoyu advertising
its own interaction modes trips that guard on every session.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Iterator, TextIO

#: JSON-RPC "method not found". What this proxy answers for kiro-only extensions.
METHOD_NOT_FOUND = -32601

#: The ``configId`` under which xiaoyu exposes model selection.
MODEL_CONFIG_ID = "model"

#: Methods answered by the proxy instead of being passed to xiaoyu. Everything
#: here is a kiro-cli extension with no standards-track equivalent; KiroCrew
#: sends them fire-and-forget and treats a rejection as "backend lacks it".
_SHORT_CIRCUIT_PREFIXES = ("_kiro.dev/",)
_SHORT_CIRCUIT_METHODS = frozenset({"_session/steer"})

#: Responses to these methods carry the session descriptor KiroCrew reads its
#: model list and agent selector out of.
_SESSION_METHODS = frozenset({"session/new", "session/load"})


def _hashable_id(value: object) -> object | None:
    """Return *value* if it is usable as a request-id key, else None.

    Request ids arrive verbatim from another process' JSON, so the spec's
    "string or number" is a claim, not a guarantee. Anything else is not tracked
    rather than raising inside the single thread that owns the wire.
    """
    return value if isinstance(value, (str, int)) else None


def models_from_config_options(options: object, fallback_current: str = "") -> dict[str, Any] | None:
    """Derive kiro's ``models`` block from xiaoyu's ``configOptions``.

    xiaoyu publishes model selection the standards-track way — a select-type
    config option in the ``model`` category. KiroCrew only reads the removed
    draft field. This reads the former and synthesizes the latter; returns None
    when no model option is present, so a response without one is left untouched
    rather than gaining an empty list KiroCrew would read as "no models".
    """
    if not isinstance(options, list):
        return None
    for option in options:
        if not isinstance(option, dict):
            continue
        if option.get("category") != MODEL_CONFIG_ID and option.get("id") != MODEL_CONFIG_ID:
            continue
        current = option.get("currentValue")
        current_id = current if isinstance(current, str) else fallback_current
        available: list[dict[str, str]] = []
        for item in option.get("options", []) or []:
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            if not isinstance(value, str) or not value:
                continue
            name = item.get("name")
            available.append(
                {
                    "modelId": value,
                    "name": name if isinstance(name, str) and name else value,
                    "description": "",
                }
            )
        if not available and not current_id:
            return None
        return {"currentModelId": current_id, "availableModels": available}
    return None


class KiroDialect:
    """Shared translation state for one stdio connection.

    Owns the real stdout and the lock that serializes writes to it. ``AcpServer``
    serializes its OWN writes with a private lock, but this proxy also emits
    frames of its own (the short-circuit replies), and those never pass through
    that lock. Without a lock here the two writers interleave mid-line and the
    client sees torn JSON.
    """

    def __init__(self, agent: str, out: TextIO) -> None:
        self._agent = agent
        self._out = out
        self._lock = threading.Lock()
        #: Ids of in-flight session/new | session/load requests, whose responses
        #: need the models block and the modes rewrite.
        self._session_ids: set[object] = set()
        #: Ids of set_model requests rewritten to set_config_option, whose
        #: responses must be rewritten back to the shape kiro expects.
        self._set_model_ids: set[object] = set()

    # ---------- outbound ----------

    def emit(self, message: dict[str, Any]) -> None:
        """Write one JSON-RPC frame to the real stdout, atomically."""
        self.emit_raw(json.dumps(message, ensure_ascii=False))

    def emit_raw(self, line: str) -> None:
        """Write one already-serialized line, atomically.

        Write and flush happen together under the lock so a frame cannot be
        split by a concurrent writer.
        """
        with self._lock:
            self._out.write(line + "\n")
            self._out.flush()

    # ---------- inbound: KiroCrew -> xiaoyu ----------

    def on_inbound(self, line: str) -> str | None:
        """Translate one client frame. Returns None when the proxy answered it.

        Unparseable input is passed through untouched so ``AcpServer`` produces
        the protocol's own parse error rather than this layer inventing one.
        """
        try:
            message = json.loads(line)
        except ValueError:
            return line
        if not isinstance(message, dict):
            return line
        method = message.get("method")
        if not isinstance(method, str):
            return line
        req_id = message.get("id")

        if method in _SHORT_CIRCUIT_METHODS or method.startswith(_SHORT_CIRCUIT_PREFIXES):
            self._reject(req_id, f"{method} is not implemented by this backend")
            return None

        if method == "session/set_mode":
            return self._on_set_mode(req_id, message)

        if method == "session/set_model":
            return self._on_set_model(req_id, message)

        if method in _SESSION_METHODS:
            self._remember(self._session_ids, req_id)

        return line

    def _reject(self, req_id: object, detail: str) -> None:
        """Answer a request with -32601. Notifications (no id) are dropped."""
        if _hashable_id(req_id) is None:
            return
        self.emit(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": METHOD_NOT_FOUND, "message": detail},
            }
        )

    def _on_set_mode(self, req_id: object, message: dict[str, Any]) -> str | None:
        """Answer ``session/set_mode``, which here selects an AGENT, not a mode.

        KiroCrew uses the ACP modes list as its agent selector, so the only mode
        this connection can honor is the agent it was spawned with. That is also
        the only one advertised (see ``_rewrite_modes``), so KiroCrew's own
        fail-closed guard normally prevents any other value from arriving.

        A different value is refused rather than acknowledged. Acknowledging it
        would leave the session running the spawned agent while KiroCrew believes
        it switched to another — which, if the requested agent is the narrower of
        the two, silently widens what the model may do.
        """
        params = message.get("params")
        mode = params.get("modeId") if isinstance(params, dict) else None
        if mode == self._agent:
            if _hashable_id(req_id) is not None:
                self.emit({"jsonrpc": "2.0", "id": req_id, "result": {}})
            return None
        self._reject(
            req_id,
            f"agent {mode!r} is not available: this backend serves only {self._agent!r} "
            f"(start a session against that agent instead)",
        )
        return None

    def _on_set_model(self, req_id: object, message: dict[str, Any]) -> str:
        """Rewrite the removed ``session/set_model`` draft call onto the standard."""
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        self._remember(self._set_model_ids, req_id)
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "session/set_config_option",
                "params": {
                    "sessionId": params.get("sessionId"),
                    "configId": MODEL_CONFIG_ID,
                    "value": params.get("modelId"),
                },
            },
            ensure_ascii=False,
        )

    # ---------- outbound: xiaoyu -> KiroCrew ----------

    def on_outbound(self, line: str) -> None:
        """Translate one agent frame and emit it."""
        try:
            message = json.loads(line)
        except ValueError:
            self.emit_raw(line)
            return
        if not isinstance(message, dict) or "result" not in message:
            self.emit_raw(line)
            return
        req_id = _hashable_id(message.get("id"))
        if req_id is not None and req_id in self._set_model_ids:
            self._set_model_ids.discard(req_id)
            # kiro expects an empty result from set_model; xiaoyu answers
            # set_config_option with the refreshed option list.
            message["result"] = {}
        elif req_id is not None and req_id in self._session_ids:
            self._session_ids.discard(req_id)
            result = message.get("result")
            if isinstance(result, dict):
                message["result"] = self._decorate_session(result)
        self.emit(message)

    def _decorate_session(self, result: dict[str, Any]) -> dict[str, Any]:
        """Add the ``models`` block and rewrite ``modes`` on a session response."""
        models = models_from_config_options(result.get("configOptions"))
        if models is not None:
            result["models"] = models
        result["modes"] = self._rewrite_modes()
        return result

    def _rewrite_modes(self) -> dict[str, Any]:
        """Advertise exactly one mode: the agent this process was spawned with.

        Advertising xiaoyu's real interaction modes would trip KiroCrew's guard,
        which tears down any session whose modes list omits the requested agent.
        Advertising every agent on disk would invite a switch this connection
        cannot perform. Naming the one true agent satisfies the guard for the
        session it can serve and makes KiroCrew fail loudly, with its own
        actionable message, for any other.
        """
        return {
            "currentModeId": self._agent,
            "availableModes": [
                {
                    "id": self._agent,
                    "name": self._agent,
                    "description": "Agent spec this backend was started with",
                }
            ],
        }

    @staticmethod
    def _remember(bucket: set[object], req_id: object) -> None:
        key = _hashable_id(req_id)
        if key is not None:
            bucket.add(key)


class DialectStdin:
    """Iterable stdin for ``AcpServer``, translating each client frame.

    ``AcpServer.serve`` consumes its stdin by ITERATION, not ``readline``, so this
    implements the iterator protocol. Frames the proxy answers itself are
    swallowed here: the loop continues to the next line rather than handing
    xiaoyu a method it would reject.
    """

    def __init__(self, dialect: KiroDialect, source: TextIO) -> None:
        self._dialect = dialect
        self._source = source

    def __iter__(self) -> Iterator[str]:
        return self

    def __next__(self) -> str:
        while True:
            raw = self._source.readline()
            if not raw:
                raise StopIteration
            line = raw.strip()
            if not line:
                continue
            translated = self._dialect.on_inbound(line)
            if translated is None:
                continue
            return translated + "\n"


class DialectStdout:
    """Write-side stdout for ``AcpServer``, translating each agent frame.

    ``AcpServer`` writes one complete frame per call, but this buffers to a
    newline anyway so a caller that writes in pieces cannot desynchronize the
    translator. ``flush`` is a no-op because every complete line is already
    flushed by ``KiroDialect.emit_raw`` under the shared lock.
    """

    def __init__(self, dialect: KiroDialect) -> None:
        self._dialect = dialect
        self._buffer = ""

    def write(self, data: str) -> int:
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if line:
                self._dialect.on_outbound(line)
        return len(data)

    def flush(self) -> None:
        return None
