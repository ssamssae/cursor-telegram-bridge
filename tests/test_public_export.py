import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

BRIDGE = Path(__file__).resolve().parents[1] / "cursor_telegram_bridge.py"


def load_bridge(alias, **env):
    base = {"CUB_DRY_RUN": "1", "CUB_CHAT_ID": "111222333"}
    base.update(env)
    with mock.patch.dict(os.environ, base, clear=False):
        spec = importlib.util.spec_from_file_location(alias, BRIDGE)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[alias] = mod
        spec.loader.exec_module(mod)
    return mod


class PublicExportTest(unittest.TestCase):
    def test_imports_public_bridge(self):
        mod = load_bridge("cursor_telegram_bridge")
        self.assertEqual(mod.CHAT_ID, "111222333")
        self.assertEqual(mod.NAME, "cursor")
        self.assertEqual(mod.TMUX_SESSION, "cursor")

    def test_chat_id_has_no_default(self):
        source = BRIDGE.read_text(encoding="utf-8")
        self.assertIn('env("CUB_CHAT_ID", "")', source)
        with mock.patch.dict(os.environ, {"CUB_DRY_RUN": "1"}, clear=False):
            os.environ.pop("CUB_CHAT_ID", None)
            spec = importlib.util.spec_from_file_location("cub_no_chat_id", BRIDGE)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["cub_no_chat_id"] = mod
            with self.assertRaises(SystemExit) as caught:
                spec.loader.exec_module(mod)
        self.assertIn("CUB_CHAT_ID", str(caught.exception))

    def test_state_dir_is_public_path(self):
        mod = load_bridge("cub_state_dir")
        self.assertIn(".cursor-telegram-bridge", mod.STATE_DIR)
        self.assertNotIn("." "claude", mod.STATE_DIR)

    def test_delivery_goes_straight_to_the_bot_api(self):
        mod = load_bridge("cub_delivery")
        self.assertFalse(hasattr(mod, "MESH_" "SEND_SH"))
        mod.DRY_RUN = False
        calls = []
        with mock.patch.object(
            mod,
            "tg",
            lambda method, timeout=60, **params: calls.append((method, params))
            or {"ok": True, "result": {"message_id": 7}},
        ):
            result = mod.deliver_mesh_event("final", "hello")
        self.assertEqual([c[0] for c in calls], ["sendMessage"])
        self.assertEqual(calls[0][1]["chat_id"], "111222333")
        self.assertEqual(calls[0][1]["text"], "hello")
        self.assertEqual(result["deliveries"][0]["message_id"], 7)

    def test_long_answer_is_chunked_under_the_telegram_cap(self):
        mod = load_bridge("cub_chunking")
        mod.DRY_RUN = False
        body = "\n".join("line %d" % i for i in range(2000))
        sent = []
        with mock.patch.object(
            mod,
            "tg",
            lambda method, timeout=60, **params: sent.append(params["text"])
            or {"ok": True, "result": {"message_id": len(sent)}},
        ):
            mod.deliver_mesh_event("final", body)
        self.assertGreater(len(sent), 1)
        for chunk in sent:
            self.assertLessEqual(len(chunk), mod.TG_CHUNK)

    def test_empty_body_sends_nothing(self):
        mod = load_bridge("cub_empty_body")
        with mock.patch.object(mod, "tg", lambda *a, **k: self.fail("must not send")):
            self.assertEqual(mod.deliver_mesh_event("final", "   "), {"deliveries": []})

    def test_turn_ended_row_is_not_busy(self):
        mod = load_bridge("cub_turn_ended")
        rows = [
            {"role": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
            {"role": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
            {"type": "turn_ended", "status": "success"},
        ]
        self.assertFalse(mod.tail_is_busy(rows))
        self.assertTrue(mod.turn_has_ended(rows))


if __name__ == "__main__":
    unittest.main()
