# @mutate-why 주기 처리에서 빠지면 답이 없는 동안 고정·만료 해제가 실행되지 않는다.
# @mutate scripts/cursor_telegram_bridge.py | sync_question_pins() | None
# @mutate-why 승인 장부에 답이 적혔는데 고정 장부만 보면(기록 사이 중단) 답한 카드가 고정된 채 남는다.
# @mutate scripts/cursor_telegram_bridge.py | and not current.get("handled") | and True
# @mutate-why 재시도 상한이 없으면 실패한 고정이 매 틱 텔레그램을 두드린다.
# @mutate scripts/cursor_telegram_bridge.py | item.get("pin_attempts", 0) >= QUESTION_PIN_MAX_ATTEMPTS | False
"""Keep unanswered Cursor question cards reachable without duplicating them (T-261107-009).

Same contract as the Codex bridge (T-261107-007): the original card of an open
approval/choice/ACP permission question stays pinned in the owner's private chat,
one bounded Telegram call per tick, and only that card is unpinned once answered,
closed, or expired. Pinning is presentation, never an answer or a permission.
"""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BRIDGE = Path(__file__).resolve().parents[1] / "cursor_telegram_bridge.py"
CHAT = "111222333"
NOW = 1789220000.0

APPROVAL_PANE = (
    "─────────────────────────────────────────\n"
    " Approval 1 of 2\n"
    "\n"
    ' $  ls -la "project" in .\n'
    "\n"
    " Run this command?\n"
    " Not in allowlist: ls\n"
    "  → Run (once) (y)\n"
    "    Add Shell(ls) to allowlist? (tab)\n"
    "    Run Everything (shift+tab)\n"
    "    Skip & tell the agent what to do instead (esc or\n"
    "  n)\n"
    "\n"
    " ←/→ to switch approval\n"
)
CHOICE_PANE = (
    " Tool server\n"
    " MCP server approval\n"
    "\n"
    " Use arrow keys to navigate, or press the key shown\n"
    "\n"
    " ▶ [a] Approve all\n"
    "   [c] Continue without\n"
    "   [q] Quit\n"
)
IDLE_PANE = "  → Plan, search, build anything\n"


def load_bridge(tmpdir):
    env = {
        "CUB_DRY_RUN": "1",
        "CUB_CHAT_ID": CHAT,
        "CUB_STATE_DIR": tmpdir,
        "CUB_NAME": "pins",
        "CUB_NODE_KEY": "pins",
        "CUB_COMPOSER_LOCK": os.path.join(tmpdir, "composer.lock"),
        "CUB_FLEET_CONTEXT_DIR": os.path.join(tmpdir, "dispatch-context"),
        "CUB_TRANSCRIPT_ROOT": os.path.join(tmpdir, "projects"),
    }
    ns = {"__file__": str(BRIDGE), "__name__": "cursor_question_pins_under_test"}
    with mock.patch.dict(os.environ, env):
        exec(compile(BRIDGE.read_text(encoding="utf-8"), str(BRIDGE), "exec"), ns)
    return ns


class Telegram:
    """Bot API stub. `mode` makes pin/unpin fail the way Telegram can: lost, refused, raise."""

    def __init__(self):
        self.calls = []
        self.pinned = {999}  # A message the user pinned by hand.
        self.mode = "ok"
        self.on_pin = None

    def __call__(self, method, timeout=60, **params):
        self.calls.append((method, timeout, params))
        if method in ("pinChatMessage", "unpinChatMessage"):
            if self.mode == "raise":
                raise TimeoutError("response lost after the operation")
            if self.mode == "lost":
                return {}
            if self.mode == "refused":
                return {"ok": True, "result": False}
            if method == "pinChatMessage":
                self.pinned.add(params["message_id"])
                if self.on_pin:
                    self.on_pin()
            else:
                self.pinned.discard(params["message_id"])
            return {"ok": True, "result": True}
        return {"ok": True, "result": {}}

    def calls_for(self, method):
        return [params for name, _timeout, params in self.calls if name == method]


class StopTick(Exception):
    pass


class Clock:
    """`time` stand-in: wall clock comes from the test, everything else is the real module."""

    def __init__(self, case):
        self.case = case

    def __getattr__(self, name):
        return getattr(real_time, name)

    def time(self):
        return self.case.now


import time as real_time  # noqa: E402


class PinCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = NOW
        self.restart()

    def restart(self):
        """Reload the bridge the way a process restart would; durable files stay."""
        self.ns = load_bridge(self.tmp.name)
        self.ns["time"] = Clock(self)
        self.telegram = Telegram()
        self.ns["tg"] = self.telegram
        self.sent = []
        self.mid = 500
        self.screen = [IDLE_PANE]
        self.keys = []
        surface = self.ns["SUGGESTED_SURFACE"]

        def deliver(kind, body, **kw):
            self.mid += 1
            self.sent.append((kind, body, kw))
            return {"deliveries": [{"surface": surface, "result": "sent", "message_id": self.mid}]}

        self.ns["deliver_mesh_event"] = deliver

        def fake_tmux(*args, input_text=None):
            if args and args[0] == "capture-pane":
                return SimpleNamespace(returncode=0, stdout=self.screen[0], stderr="")
            if args and args[0] == "send-keys":
                self.keys.append(args[-1])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        self.ns["_tmux"] = fake_tmux
        self.ns["tui_session_alive"] = lambda: True

    # helpers --------------------------------------------------------------
    def tick(self, n=1):
        return [self.ns["sync_question_pins"](self.now) for _ in range(n)]

    def store(self):
        return self.ns["_read_question_pin_store"]()

    def show_approval(self):
        self.screen[0] = APPROVAL_PANE
        self.assertEqual(self.ns["surface_tui_approval"](), 1)
        return self.ns["_read_approval_store"]()

    def show_choice(self):
        self.screen[0] = CHOICE_PANE
        self.assertEqual(self.ns["surface_tui_choice_menu"](), 1)
        return self.ns["_read_choice_store"]()

    def press_approval(self, card, option="once"):
        self.screen[0] = APPROVAL_PANE
        self.ns["handle_telegram_callback"]({
            "id": "q", "from": {"id": int(CHAT)},
            "data": f"{self.ns['APPROVAL_CALLBACK_PREFIX']}:{card['cid']}:{option}",
            "message": {"chat": {"id": int(CHAT)}, "message_id": card["message_id"]},
        })

    def press_choice(self, card, key="a"):
        self.screen[0] = CHOICE_PANE
        self.ns["handle_telegram_callback"]({
            "id": "q", "from": {"id": int(CHAT)},
            "data": f"{self.ns['CHOICE_CALLBACK_PREFIX']}:{card['cid']}:{key}",
            "message": {"chat": {"id": int(CHAT)}, "message_id": card["message_id"]},
        })

    def pins(self):
        return self.telegram.calls_for("pinChatMessage")

    def unpins(self):
        return self.telegram.calls_for("unpinChatMessage")


class QuestionPinsTest(PinCase):
    def test_open_approval_card_is_pinned_silently_once(self):
        card = self.show_approval()
        self.assertEqual(self.tick(), ["pinChatMessage"])
        self.assertEqual(self.pins(), [{"chat_id": CHAT, "message_id": card["message_id"],
                                        "disable_notification": "true"}])
        self.assertEqual(self.telegram.calls[0][1], 5)  # bounded request timeout
        self.assertTrue(self.store()[f"approval:{card['cid']}"]["pin_confirmed"])
        self.tick(3)
        self.assertEqual(len(self.pins()), 1)
        self.assertEqual(self.ns["surface_tui_approval"](), 0)  # same modal: no second card
        self.assertEqual(len(self.sent), 1)

    def test_two_open_cards_are_both_pinned_and_an_answer_unpins_only_that_card(self):
        approval = self.show_approval()
        self.tick()
        choice = self.show_choice()
        self.tick()
        self.assertEqual([p["message_id"] for p in self.pins()],
                         [approval["message_id"], choice["message_id"]])
        self.press_choice(choice, "a")
        self.assertEqual(self.keys, ["a"])
        self.assertEqual(self.tick(), ["unpinChatMessage"])
        self.assertEqual(self.unpins(), [{"chat_id": CHAT, "message_id": choice["message_id"]}])
        self.assertEqual(self.tick(), [None])
        self.assertEqual(set(self.store()), {f"approval:{approval['cid']}"})
        self.assertEqual(self.telegram.pinned, {999, approval["message_id"]})

    def test_closed_card_unpins_when_the_modal_disappears(self):
        card = self.show_approval()
        self.tick()
        self.screen[0] = IDLE_PANE
        self.assertEqual(self.ns["surface_tui_approval"](), 0)  # modal gone: store cleared
        self.assertEqual(self.tick(), ["unpinChatMessage"])
        self.assertEqual(self.store(), {})
        self.assertEqual(self.telegram.pinned, {999})
        self.assertFalse(self.telegram.calls_for("unpinAllChatMessages"))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.unpins()[0]["message_id"], card["message_id"])

    def test_expiry_unpins_even_with_no_new_events(self):
        self.show_approval()
        self.tick()
        self.now += self.ns["QUESTION_PIN_TTL"]
        self.assertEqual(self.tick(), ["unpinChatMessage"])
        self.assertEqual(self.store(), {})

    def test_restart_keeps_confirmed_pin_without_repinning_or_resending(self):
        self.show_approval()
        self.tick()
        self.restart()
        self.screen[0] = APPROVAL_PANE
        self.assertEqual(self.ns["surface_tui_approval"](), 0)
        self.assertEqual(self.tick(2), [None, None])
        self.assertEqual(self.telegram.calls, [])
        self.assertEqual(self.sent, [])

    def test_answer_recorded_before_a_crash_is_released_after_restart(self):
        # The callback writes the answer to the approval store first and the pin
        # bookkeeping second. A crash between the two leaves `handled` on disk while
        # the pin entry still says "open": the live store decides, not the pin entry.
        card = self.show_approval()
        self.tick()
        answered = self.ns["_read_approval_store"]()
        answered["handled"] = True
        self.ns["_write_approval_store"](answered)
        self.restart()
        self.screen[0] = APPROVAL_PANE
        self.assertEqual(self.store()[f"approval:{card['cid']}"]["status"], "open")
        self.assertEqual(self.tick(), ["unpinChatMessage"])
        self.assertEqual(self.unpins()[0]["message_id"], card["message_id"])
        self.assertEqual(self.store(), {})
        self.assertEqual(self.sent, [])

    def test_pin_failures_are_bounded_across_restart_and_never_block_answer(self):
        self.telegram.mode = "lost"
        card = self.show_approval()
        self.tick(5)
        self.assertEqual(len(self.pins()), 1)  # one attempt, then the 60 s hold
        self.now += 60
        self.tick()
        self.assertEqual(len(self.pins()), 2)
        self.restart()
        self.telegram.mode = "lost"
        self.screen[0] = APPROVAL_PANE
        self.now += 60
        self.tick()
        self.assertEqual(len(self.pins()), 1)  # third attempt, counted across the restart
        self.now += 60
        self.tick(3)
        self.assertEqual(len(self.pins()), 1)  # cap reached: no fourth attempt
        item = self.store()[f"approval:{card['cid']}"]
        self.assertEqual((item["pin_attempts"], item["pin_confirmed"]), (3, None))
        self.press_approval(card, "once")
        self.assertEqual(self.keys, ["y"])
        self.assertTrue(self.ns["_read_approval_store"]()["handled"])

    def test_unpin_failure_retries_without_reopening_or_resubmitting(self):
        card = self.show_approval()
        self.tick()
        self.screen[0] = IDLE_PANE
        self.ns["surface_tui_approval"]()
        self.telegram.mode = "lost"
        self.assertEqual(self.tick(3), ["unpinChatMessage", None, None])
        self.assertIn(f"approval:{card['cid']}", self.store())
        self.now += 60
        self.telegram.mode = "ok"
        self.assertEqual(self.tick(), ["unpinChatMessage"])
        self.assertEqual(self.store(), {})
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.ns["_read_approval_store"](), {})

    def test_no_pin_for_invalid_id_group_chat_or_foreign_chat(self):
        register = self.ns["register_question_pin"]
        for bad in (None, 0, -5, "x", True):
            self.assertFalse(register("approval:bad", bad))
        self.assertEqual(self.store(), {})
        surface = self.ns["SUGGESTED_SURFACE"]
        self.ns["deliver_mesh_event"] = lambda kind, body, **kw: {
            "deliveries": [{"surface": surface, "result": "sent"}]}  # no message id
        self.show_approval()
        self.assertEqual(self.tick(), [None])
        self.assertTrue(register("acp:foreign", 77, chat_id="999"))
        self.assertEqual(self.tick(), [None])
        self.ns["CHAT_ID"] = "-100123"
        self.assertTrue(register("acp:group", 78, chat_id="-100123"))
        self.assertEqual(self.tick(), [None])
        self.assertEqual(self.telegram.calls, [])

    def test_one_network_operation_per_tick_and_no_global_unpin(self):
        self.show_approval()
        self.show_choice()
        self.assertEqual(self.tick(), ["pinChatMessage"])
        self.assertEqual(len(self.telegram.calls), 1)
        self.assertEqual(self.tick(), ["pinChatMessage"])
        self.assertEqual(self.tick(), [None])
        self.now += self.ns["QUESTION_PIN_TTL"]
        self.assertEqual(self.tick(3), ["unpinChatMessage", "unpinChatMessage", None])
        self.assertFalse(self.telegram.calls_for("unpinAllChatMessages"))
        self.assertEqual(self.telegram.pinned, {999})

    def test_unsuccessful_telegram_response_is_not_recorded_as_pinned(self):
        self.telegram.mode = "refused"
        card = self.show_approval()
        self.tick()
        item = self.store()[f"approval:{card['cid']}"]
        self.assertEqual((item["pin_attempts"], item["pin_confirmed"]), (1, None))
        self.telegram.mode = "raise"
        self.now += 60
        self.tick()
        item = self.store()[f"approval:{card['cid']}"]
        self.assertEqual((item["pin_attempts"], item["pin_confirmed"]), (2, None))
        self.assertNotIn(card["message_id"], self.telegram.pinned)

    def test_answer_during_pin_request_is_unpinned_on_next_tick(self):
        card = self.show_approval()
        self.telegram.on_pin = lambda: self.press_approval(card, "once")  # answered mid-request
        self.assertEqual(self.tick(), ["pinChatMessage"])
        self.assertEqual(self.keys, ["y"])
        self.assertEqual(self.tick(), ["unpinChatMessage"])
        self.assertEqual(self.store(), {})

    def test_ticker_iteration_surfaces_the_card_and_pins_it(self):
        self.screen[0] = APPROVAL_PANE

        def sleep(_seconds):
            raise StopTick()

        self.ns["time"].sleep = sleep
        with self.assertRaises(StopTick):
            self.ns["approval_surface_ticker"]()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.pins()), 1)
        self.assertTrue(next(iter(self.store().values()))["pin_confirmed"])


try:
    sys.path.insert(0, str(BRIDGE.parent))
    acp_module = importlib.import_module("cursor_acp_bridge")
except Exception:  # noqa: BLE001 — the public export ships the bridge without the ACP lane.
    acp_module = None


@unittest.skipUnless(acp_module, "ACP lane not present")
class AcpPermissionPinsTest(PinCase):
    def test_acp_permission_card_is_pinned_while_open_and_released_when_answered_or_closed(self):
        ns = self.ns
        outbox = Path(self.tmp.name) / "outbox"
        events = {}

        def fake_call(socket, request, event=None):
            if request.get("method") == "permission":
                return {"optionId": "allow"}
            if event is not None:
                event({"data": {"sessionUpdate": "permission", "requestId": "req-1",
                                "toolCall": {"title": "ls"},
                                "options": [{"optionId": "allow", "name": "Allow"}]}})
                events["ticks"] = self.tick(2)  # the ACP loop keeps syncing while the prompt runs
                if events.get("answer"):
                    self.bridge.callback("acp:req-1:0")
                    events["after_answer"] = self.tick()
            return {"stopReason": "end_turn", "text": "done"}

        with mock.patch.object(acp_module, "call", fake_call):
            self.bridge = acp_module.AcpBridge("unused", outbox, ns["deliver_mesh_event"],
                                               mock.Mock(), pins=ns["_QuestionPinHooks"])
            self.bridge.run(self.bridge.prepare("hello", 7))
            self.assertEqual(events["ticks"], ["pinChatMessage", None])
            self.assertEqual(self.pins()[0]["message_id"], 501)
            self.assertEqual(self.store()["acp:req-1"]["status"], "closed")  # prompt ended
            self.assertEqual(self.tick(), ["unpinChatMessage"])
            self.assertEqual(self.store(), {})

            events.clear()
            events["answer"] = True
            self.bridge.run(self.bridge.prepare("again", 8))
            self.assertEqual(events["ticks"], ["pinChatMessage", None])
            self.assertEqual(events["after_answer"], ["unpinChatMessage"])
            self.assertEqual(self.store(), {})
            self.assertEqual(self.telegram.pinned, {999})


if __name__ == "__main__":
    unittest.main()
