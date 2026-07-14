import threading
import unittest
from types import SimpleNamespace

from core import wechat_ui_actions
from wxbot_core import WXBot


def msg(msg_type, content):
    return SimpleNamespace(type=msg_type, content=content)


class RecordingOwner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def call(self, intent, timeout):
        self.calls.append((intent, timeout))
        return self.result


class RecordingStorage:
    def __init__(self, materials):
        self.materials = list(materials)
        self.saved = []

    def load(self):
        return list(self.materials)

    def save(self, materials):
        self.saved.append(list(materials))


class RecordingLock:
    def __init__(self, name, events):
        self.name = name
        self.events = events

    def __enter__(self):
        self.events.append(f"enter:{self.name}")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.events.append(f"exit:{self.name}")
        return False


def make_bot(owner):
    bot = WXBot.__new__(WXBot)
    bot._ui_owner = owner
    bot._material_source_read_strategies = {}
    bot._material_source_read_locks = {}
    bot._material_source_read_locks_guard = threading.Lock()
    return bot


class MaterialSourceReaderTests(unittest.TestCase):
    def test_read_submits_owner_intent_and_records_strategy(self):
        messages = [msg("link", "[链接]素材")]
        owner = RecordingOwner({"messages": messages, "strategy": "子窗口历史"})
        bot = make_bot(owner)

        result = bot._read_material_source_messages(" 素材源 ", 5, goback=False)

        self.assertEqual(result, messages)
        self.assertEqual(bot._material_source_read_strategy("素材源"), "子窗口历史")
        self.assertEqual(len(owner.calls), 1)
        intent, timeout = owner.calls[0]
        self.assertEqual(intent.kind, wechat_ui_actions.UIIntentKind.MATERIAL_READ)
        self.assertEqual(
            intent.payload,
            {
                "conversation": "素材源",
                "limit": 5,
                "goback": False,
                "target_signature": "",
                "require_forwardable": True,
            },
        )
        self.assertEqual(timeout, wechat_ui_actions.UI_CALL_WAIT_TIMEOUT)

    def test_read_passes_targeted_refresh_options_to_owner(self):
        owner = RecordingOwner({"messages": [], "strategy": "定向读取"})
        bot = make_bot(owner)

        bot._read_material_source_messages(
            "素材源",
            0,
            target_signature="link|目标",
            require_forwardable=False,
        )

        intent, _timeout = owner.calls[0]
        self.assertEqual(intent.payload["limit"], 1)
        self.assertEqual(intent.payload["target_signature"], "link|目标")
        self.assertFalse(intent.payload["require_forwardable"])

    def test_rebuild_keeps_existing_materials_when_owner_finds_no_forwardable_messages(self):
        existing = {
            "id": "mat_old",
            "source": "素材源",
            "type": "link",
            "type_bucket": "link",
            "content_preview": "[链接]旧素材",
            "stable_signature": "link|[链接]旧素材",
            "created_at": "2026-06-01T10:00:00",
            "status": "active",
        }
        owner = RecordingOwner({"messages": [msg("text", "只是文本")], "strategy": "子窗口历史"})
        bot = make_bot(owner)
        bot.config = SimpleNamespace(material_source_pool_limit_map={})
        bot._material_runtime_messages = {}
        storage = RecordingStorage([existing])
        bot._load_material_outreach_materials = storage.load
        bot._save_material_outreach_materials = storage.save

        materials = bot._rebuild_material_runtime_pool_for_source("素材源", goback=True)

        self.assertEqual(materials, [existing])
        self.assertEqual(storage.saved, [])
        self.assertEqual(bot._material_runtime_messages, {})

    def test_forward_uses_source_lock_and_owner_path(self):
        bot = make_bot(object())
        events = []
        bot._material_source_read_locks = {"素材源": RecordingLock("source", events)}
        bot._ui_forward_message = lambda _chat, _message, targets, **_kwargs: events.append(
            "owner:" + ",".join(targets)
        ) or True

        class RawMessage:
            type = "link"
            attr = "friend"
            sender = "素材源"
            content = "素材"

            def roll_into_view(self):
                raise AssertionError("业务线程不得操作原始消息")

            def forward(self, *_args, **_kwargs):
                raise AssertionError("业务线程不得直接转发")

        success, error = bot._forward_material_message(
            RawMessage(),
            ["阿英2"],
            material_source="素材源",
        )

        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertEqual(events, ["enter:source", "owner:阿英2", "exit:source"])


if __name__ == "__main__":
    unittest.main()
