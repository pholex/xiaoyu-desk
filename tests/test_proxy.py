"""Dialect translation tests: feed frames in, assert what comes out."""

from __future__ import annotations

import io
import json
import threading
import unittest

from xiaoyu_desk.acp.proxy import (
    METHOD_NOT_FOUND,
    DialectStdin,
    DialectStdout,
    KiroDialect,
    models_from_config_options,
)

AGENT = "kirocrew"


def _config_options(current: str = "deepseek-chat", values=("deepseek-chat", "kimi-k2")):
    return [
        {
            "id": "model",
            "name": "模型",
            "category": "model",
            "type": "select",
            "currentValue": current,
            "options": [{"value": v, "name": v} for v in values],
        }
    ]


class _Harness:
    """One dialect wired to in-memory stdio, plus helpers to drive both sides."""

    def __init__(self, agent: str = AGENT):
        self.out = io.StringIO()
        self.dialect = KiroDialect(agent, self.out)
        self.stdout = DialectStdout(self.dialect)

    def inbound(self, message: dict) -> dict | None:
        """Push a client frame; return what xiaoyu would see, or None."""
        line = self.dialect.on_inbound(json.dumps(message))
        return None if line is None else json.loads(line)

    def outbound(self, message: dict) -> None:
        """Push an agent frame through the stdout adapter."""
        self.stdout.write(json.dumps(message) + "\n")

    def frames(self) -> list[dict]:
        return [json.loads(line) for line in self.out.getvalue().splitlines() if line.strip()]


class TestModelTranslation(unittest.TestCase):
    def test_set_model_is_rewritten_to_set_config_option(self):
        h = _Harness()
        seen = h.inbound(
            {"jsonrpc": "2.0", "id": 7, "method": "session/set_model",
             "params": {"sessionId": "s1", "modelId": "kimi-k2"}}
        )
        self.assertEqual(seen["method"], "session/set_config_option")
        self.assertEqual(
            seen["params"],
            {"sessionId": "s1", "configId": "model", "value": "kimi-k2"},
        )
        self.assertEqual(seen["id"], 7)

    def test_set_config_option_reply_is_rewritten_back_to_empty(self):
        h = _Harness()
        h.inbound(
            {"jsonrpc": "2.0", "id": 7, "method": "session/set_model",
             "params": {"sessionId": "s1", "modelId": "kimi-k2"}}
        )
        # xiaoyu answers set_config_option with the refreshed option list; the
        # client asked set_model and expects an empty result.
        h.outbound({"jsonrpc": "2.0", "id": 7, "result": {"configOptions": _config_options()}})
        self.assertEqual(h.frames()[0]["result"], {})

    def test_session_new_reply_gains_models_derived_from_config_options(self):
        h = _Harness()
        h.inbound({"jsonrpc": "2.0", "id": 1, "method": "session/new",
                   "params": {"cwd": "/tmp"}})
        h.outbound(
            {"jsonrpc": "2.0", "id": 1,
             "result": {"sessionId": "sess-abc", "configOptions": _config_options()}}
        )
        models = h.frames()[0]["result"]["models"]
        self.assertEqual(models["currentModelId"], "deepseek-chat")
        self.assertEqual(
            [m["modelId"] for m in models["availableModels"]],
            ["deepseek-chat", "kimi-k2"],
        )

    def test_models_and_config_options_agree(self):
        options = _config_options(current="kimi-k2")
        models = models_from_config_options(options)
        self.assertEqual(
            {m["modelId"] for m in models["availableModels"]},
            {o["value"] for o in options[0]["options"]},
        )
        self.assertEqual(models["currentModelId"], options[0]["currentValue"])

    def test_no_model_option_leaves_response_untouched(self):
        # An empty list would read as "this account has no models" and withhold
        # the picker; absent means "unknown", which the client handles.
        self.assertIsNone(models_from_config_options([{"id": "effort", "category": "effort"}]))
        h = _Harness()
        h.inbound({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}})
        h.outbound({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "s", "configOptions": []}})
        self.assertNotIn("models", h.frames()[0]["result"])


class TestModeTranslation(unittest.TestCase):
    def test_session_new_modes_are_replaced_by_the_spawned_agent(self):
        h = _Harness()
        h.inbound({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}})
        h.outbound(
            {"jsonrpc": "2.0", "id": 1,
             "result": {"sessionId": "s",
                        "modes": {"currentModeId": "default",
                                  "availableModes": [{"id": "default", "name": "默认"},
                                                     {"id": "auto", "name": "自动"},
                                                     {"id": "plan", "name": "规划"}]}}}
        )
        modes = h.frames()[0]["result"]["modes"]
        self.assertEqual(modes["currentModeId"], AGENT)
        self.assertEqual([m["id"] for m in modes["availableModes"]], [AGENT])

    def test_set_mode_for_the_spawned_agent_is_acknowledged(self):
        h = _Harness()
        self.assertIsNone(
            h.inbound({"jsonrpc": "2.0", "id": 3, "method": "session/set_mode",
                       "params": {"sessionId": "s", "modeId": AGENT}})
        )
        self.assertEqual(h.frames(), [{"jsonrpc": "2.0", "id": 3, "result": {}}])

    def test_set_mode_for_another_agent_is_refused_not_faked(self):
        # Acknowledging a switch that did not happen would leave the session on
        # the spawned agent while the client believes it moved to the requested
        # one — silently widening what the model may do when that one is narrower.
        h = _Harness()
        self.assertIsNone(
            h.inbound({"jsonrpc": "2.0", "id": 4, "method": "session/set_mode",
                       "params": {"sessionId": "s", "modeId": "some-app-agent"}})
        )
        frame = h.frames()[0]
        self.assertEqual(frame["error"]["code"], METHOD_NOT_FOUND)
        self.assertIn("some-app-agent", frame["error"]["message"])


class TestShortCircuit(unittest.TestCase):
    def test_kiro_extensions_are_answered_here_and_not_forwarded(self):
        for method in ("_kiro.dev/session/terminate", "_kiro.dev/metadata", "_session/steer"):
            with self.subTest(method=method):
                h = _Harness()
                self.assertIsNone(
                    h.inbound({"jsonrpc": "2.0", "id": 9, "method": method, "params": {}})
                )
                self.assertEqual(h.frames()[0]["error"]["code"], METHOD_NOT_FOUND)

    def test_extension_notification_is_dropped_without_a_reply(self):
        # No id means a notification; answering one would be a protocol error.
        h = _Harness()
        self.assertIsNone(h.inbound({"jsonrpc": "2.0", "method": "_kiro.dev/clear/status"}))
        self.assertEqual(h.frames(), [])

    def test_ordinary_methods_pass_through_untouched(self):
        h = _Harness()
        message = {"jsonrpc": "2.0", "id": 2, "method": "session/prompt",
                   "params": {"sessionId": "s", "prompt": [{"type": "text", "text": "hi"}]}}
        self.assertEqual(h.inbound(message), message)
        self.assertEqual(h.frames(), [])


class TestWireRobustness(unittest.TestCase):
    def test_unparseable_input_is_left_for_the_server_to_reject(self):
        h = _Harness()
        self.assertEqual(h.dialect.on_inbound("{not json"), "{not json")

    def test_notifications_stream_through_unchanged(self):
        h = _Harness()
        update = {"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": "s", "update": {"sessionUpdate": "agent_message_chunk"}}}
        h.outbound(update)
        self.assertEqual(h.frames(), [update])

    def test_stdin_swallows_short_circuited_frames_and_yields_the_rest(self):
        raw = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "_kiro.dev/metadata"}),
            "",
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {}}),
        ]) + "\n"
        h = _Harness()
        lines = list(DialectStdin(h.dialect, io.StringIO(raw)))
        self.assertEqual([json.loads(line)["method"] for line in lines], ["session/prompt"])

    def test_partial_writes_are_buffered_to_whole_frames(self):
        h = _Harness()
        h.stdout.write('{"jsonrpc": "2.0", "method": "session/')
        self.assertEqual(h.frames(), [])
        h.stdout.write('update", "params": {}}\n')
        self.assertEqual(h.frames()[0]["method"], "session/update")

    def test_concurrent_writers_do_not_tear_frames(self):
        # The server serializes its own writes with a private lock; short-circuit
        # replies bypass that lock entirely, so the shared one here is what keeps
        # the two from interleaving mid-line.
        h = _Harness()
        errors: list[BaseException] = []

        def hammer(kind: int) -> None:
            try:
                for i in range(200):
                    if kind:
                        h.dialect.emit({"jsonrpc": "2.0", "id": f"a{i}", "result": {}})
                    else:
                        h.outbound({"jsonrpc": "2.0", "method": "session/update",
                                    "params": {"n": i}})
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(k,)) for k in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        # Every line must parse: a torn frame is exactly what fails here.
        self.assertEqual(len(h.frames()), 400)


if __name__ == "__main__":
    unittest.main()
