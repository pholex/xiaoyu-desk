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
        for method in ("_kiro.dev/session/terminate", "_kiro.dev/metadata"):
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


class _FakeAgent:
    def __init__(self):
        self.steered: list[str] = []

    def steer(self, text: str) -> None:
        self.steered.append(text)


class _FakeServer:
    """Stands in for AcpServer's embedding surface."""

    def __init__(self, sessions: dict):
        self.sessions = sessions
        self.lookups: list[str] = []

    def agent_for(self, session_id: str):
        self.lookups.append(session_id)
        return self.sessions.get(session_id)


def _steer(session_id="s1", message="<user_message>\nuse pytest\n</user_message>", req_id=5):
    frame = {"jsonrpc": "2.0", "method": "_session/steer",
             "params": {"sessionId": session_id, "message": message}}
    if req_id is not None:
        frame["id"] = req_id
    return frame


class TestSteer(unittest.TestCase):
    """Mid-turn steer: the one KiroCrew feature that used to silently do nothing.

    ACP has no steer method, so this is a kiro extension translated onto
    Agent.steer. The rule throughout is that a steer either lands or is refused
    — never acknowledged into the void, which is what the old -32601 amounted to
    from the user's side (KiroCrew treats the rejection as fire-and-forget and
    shows nothing).
    """

    def _bound(self, sessions=None):
        agent = _FakeAgent()
        server = _FakeServer(sessions if sessions is not None else {"s1": agent})
        h = _Harness()
        h.dialect.bind(server)
        return h, server, agent

    def test_the_envelope_is_stripped_before_the_model_sees_it(self):
        # KiroCrew wraps the text in <user_message>…</user_message>. Passed
        # through verbatim the model reads the tags as part of the instruction.
        h, _server, agent = self._bound()
        self.assertIsNone(h.inbound(_steer()))
        self.assertEqual(agent.steered, ["use pytest"])

    def test_text_without_the_envelope_still_steers(self):
        # A hand-rolled client, or a changed envelope, must not stop a steer.
        h, _server, agent = self._bound()
        h.inbound(_steer(message="just do it"))
        self.assertEqual(agent.steered, ["just do it"])

    def test_a_consumed_echo_is_emitted_so_the_ui_can_settle(self):
        # Without this KiroCrew's steer card stays pending forever: the reply is
        # not what it settles on, the notification is.
        h, _server, _agent = self._bound()
        h.inbound(_steer())
        updates = [f for f in h.frames() if f.get("method") == "session/update"]
        self.assertEqual(len(updates), 1)
        update = updates[0]["params"]["update"]
        self.assertEqual(update["sessionUpdate"], "steering_consumed")
        self.assertEqual(update["content"], "use pytest")
        self.assertEqual(updates[0]["params"]["sessionId"], "s1")

    def test_the_request_is_answered_queued(self):
        h, _server, _agent = self._bound()
        h.inbound(_steer())
        replies = [f for f in h.frames() if f.get("id") == 5]
        self.assertEqual(replies[0]["result"], {"queued": True})

    def test_an_unknown_session_is_refused_and_steers_nothing(self):
        h, _server, agent = self._bound(sessions={})
        h.inbound(_steer(session_id="ghost"))
        self.assertEqual(agent.steered, [])
        self.assertEqual(h.frames()[0]["error"]["code"], METHOD_NOT_FOUND)
        self.assertNotIn("session/update", [f.get("method") for f in h.frames()])

    def test_an_empty_steer_is_refused_rather_than_queued(self):
        h, _server, agent = self._bound()
        h.inbound(_steer(message="<user_message>\n\n</user_message>"))
        self.assertEqual(agent.steered, [])
        self.assertEqual(h.frames()[0]["error"]["code"], METHOD_NOT_FOUND)

    def test_an_unbound_dialect_refuses_instead_of_pretending(self):
        # Before bind() there is no way to reach a session. Answering ok here
        # would be the exact silent no-op this route replaced.
        h = _Harness()
        self.assertIsNone(h.inbound(_steer()))
        self.assertEqual(h.frames()[0]["error"]["code"], METHOD_NOT_FOUND)

    def test_the_agent_is_looked_up_fresh_every_time(self):
        # session/load replaces a session's Agent; a cached one would steer a
        # conversation nobody is watching.
        h, server, _agent = self._bound()
        h.inbound(_steer(req_id=1))
        h.inbound(_steer(req_id=2))
        self.assertEqual(server.lookups, ["s1", "s1"])

    def test_steer_never_reaches_xiaoyu_on_the_wire(self):
        # xiaoyu would answer "method not found"; the translation is the point.
        h, _server, _agent = self._bound()
        self.assertIsNone(h.inbound(_steer()))


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
