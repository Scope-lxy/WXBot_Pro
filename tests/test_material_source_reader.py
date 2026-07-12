import threading
import unittest
from types import SimpleNamespace

from wxbot_core import WXBot


def msg(msg_type, content):
    return SimpleNamespace(type=msg_type, content=content)


class FakeChatBox:
    def __init__(self, messages=None, error=None):
        self.messages = list(messages or [])
        self.error = error
        self.calls = []

    def get_msgs_from_history(self, n, callback=None, interval=0.2, speed=1, goback=True):
        self.calls.append(
            {
                "n": n,
                "callback": callback,
                "interval": interval,
                "speed": speed,
                "goback": goback,
            }
        )
        if self.error:
            raise self.error
        return list(self.messages)


class FakeSourceChat:
    def __init__(self, chat_box=None, visible_messages=None):
        self.ChatBox = chat_box
        self.visible_messages = list(visible_messages or [])

    def GetAllMessage(self):
        return list(self.visible_messages)


class FakeSourceChatWithPublicHistory(FakeSourceChat):
    def __init__(self, chat_box=None, visible_messages=None, public_history_messages=None):
        super().__init__(chat_box=chat_box, visible_messages=visible_messages)
        self.public_history_messages = list(public_history_messages or [])
        self.public_history_calls = []

    def GetHistoryMessage(self, n, callback=None, interval=0.2, speed=1, goback=True):
        self.public_history_calls.append(
            {
                "n": n,
                "callback": callback,
                "interval": interval,
                "speed": speed,
                "goback": goback,
            }
        )
        return list(self.public_history_messages)


class FakeMainWindow:
    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.chat_with_calls = []
        self.history_calls = []

    def ChatWith(self, source, exact=True):
        self.chat_with_calls.append((source, exact))
        return source

    def GetHistoryMessage(self, n, callback=None, interval=0.2, speed=1, goback=True):
        self.history_calls.append(
            {
                "n": n,
                "callback": callback,
                "interval": interval,
                "speed": speed,
                "goback": goback,
            }
        )
        return list(self.messages)


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


class FakeForwardMessage:
    def __init__(self, events):
        self.events = events

    def roll_into_view(self):
        self.events.append("roll")

    def forward(self, targets):
        self.events.append(f"forward:{','.join(targets)}")
        return {"status": "成功", "message": "成功", "data": None}


def make_bot(source_chat, main_window):
    bot = WXBot.__new__(WXBot)
    bot.wx = main_window
    bot._material_source_chats = {"素材源": source_chat}
    bot._material_source_read_strategies = {}
    bot._material_source_read_locks = {}
    bot._material_source_read_locks_guard = threading.Lock()
    bot._wechat_action_lock = threading.RLock()
    return bot


class MaterialSourceReaderTests(unittest.TestCase):
    def test_prefers_subwindow_internal_history_before_main_window(self):
        source_messages = [msg("link", "[链接]子窗口素材")]
        source_chat = FakeSourceChat(chat_box=FakeChatBox(source_messages))
        main_window = FakeMainWindow([msg("link", "[链接]主窗口素材")])
        bot = make_bot(source_chat, main_window)

        messages = bot._read_material_source_messages("素材源", 5, goback=True)

        self.assertIs(messages[0], source_messages[0])
        self.assertEqual(len(source_chat.ChatBox.calls), 1)
        self.assertEqual(main_window.chat_with_calls, [])
        self.assertEqual(
            bot._material_source_read_strategy("素材源"),
            "子窗口内部 ChatBox.get_msgs_from_history",
        )

    def test_material_source_read_uses_domain_lock_without_legacy_global_lock(self):
        events = []
        source_messages = [msg("link", "[链接]子窗口素材")]
        source_chat = FakeSourceChat(chat_box=FakeChatBox(source_messages))
        bot = make_bot(source_chat, FakeMainWindow([msg("link", "[链接]主窗口素材")]))
        bot._material_source_read_locks = {"素材源": RecordingLock("source", events)}

        messages = bot._read_material_source_messages("素材源", 5, goback=True)

        self.assertIs(messages[0], source_messages[0])
        self.assertEqual(
            events,
            ["enter:source", "exit:source"],
        )

    def test_prefers_subwindow_internal_history_before_subwindow_public_history(self):
        internal_messages = [msg("link", "[链接]内部素材")]
        public_messages = [msg("link", "[链接]公开素材")]
        source_chat = FakeSourceChatWithPublicHistory(
            chat_box=FakeChatBox(internal_messages),
            public_history_messages=public_messages,
        )
        main_window = FakeMainWindow([msg("link", "[链接]主窗口素材")])
        bot = make_bot(source_chat, main_window)

        messages = bot._read_material_source_messages("素材源", 5, goback=True)

        self.assertIs(messages[0], internal_messages[0])
        self.assertEqual(len(source_chat.ChatBox.calls), 1)
        self.assertEqual(source_chat.public_history_calls, [])
        self.assertEqual(main_window.chat_with_calls, [])
        self.assertEqual(
            bot._material_source_read_strategy("素材源"),
            "子窗口内部 ChatBox.get_msgs_from_history",
        )

    def test_falls_back_to_subwindow_public_history_before_main_window(self):
        public_messages = [msg("link", "[链接]公开素材")]
        source_chat = FakeSourceChatWithPublicHistory(
            chat_box=FakeChatBox(error=RuntimeError("内部失效")),
            public_history_messages=public_messages,
        )
        main_window = FakeMainWindow([msg("link", "[链接]主窗口素材")])
        bot = make_bot(source_chat, main_window)

        messages = bot._read_material_source_messages("素材源", 5, goback=True)

        self.assertIs(messages[0], public_messages[0])
        self.assertEqual(len(source_chat.ChatBox.calls), 1)
        self.assertEqual(len(source_chat.public_history_calls), 1)
        self.assertEqual(main_window.chat_with_calls, [])
        self.assertEqual(
            bot._material_source_read_strategy("素材源"),
            "子窗口公开 GetHistoryMessage",
        )

    def test_falls_back_to_main_window_when_subwindow_internal_history_fails(self):
        source_chat = FakeSourceChat(
            chat_box=FakeChatBox(error=RuntimeError("子窗口失效")),
            visible_messages=[msg("link", "[链接]可见兜底")],
        )
        main_messages = [msg("link", "[链接]主窗口素材")]
        main_window = FakeMainWindow(main_messages)
        bot = make_bot(source_chat, main_window)

        messages = bot._read_material_source_messages("素材源", 5, goback=False)

        self.assertIs(messages[0], main_messages[0])
        self.assertEqual(main_window.chat_with_calls, [("素材源", True)])
        self.assertEqual(len(main_window.history_calls), 1)
        self.assertEqual(
            bot._material_source_read_strategy("素材源"),
            "主窗口公开 GetHistoryMessage",
        )

    def test_rebuild_keeps_existing_materials_when_reads_have_no_forwardable_messages(self):
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
        source_chat = FakeSourceChat(
            chat_box=FakeChatBox([msg("text", "只是文本")]),
            visible_messages=[msg("text", "可见文本")],
        )
        main_window = FakeMainWindow([msg("text", "主窗口文本")])
        bot = make_bot(source_chat, main_window)
        bot.config = SimpleNamespace(material_source_pool_limit_map={})
        bot._material_runtime_messages = {}
        storage = RecordingStorage([existing])
        bot._load_material_outreach_materials = storage.load
        bot._save_material_outreach_materials = storage.save

        materials = bot._rebuild_material_runtime_pool_for_source("素材源", goback=True)

        self.assertEqual(materials, [existing])
        self.assertEqual(storage.saved, [])
        self.assertEqual(bot._material_runtime_messages, {})

    def test_forward_uses_source_lock_without_legacy_global_lock(self):
        bot = WXBot.__new__(WXBot)
        events = []
        bot._material_source_read_locks = {"素材源": RecordingLock("source", events)}
        bot._material_source_read_locks_guard = threading.Lock()

        success, error = bot._forward_material_message(
            FakeForwardMessage(events),
            ["阿英2"],
            material_source="素材源",
        )

        self.assertTrue(success)
        self.assertEqual(
            events,
            ["enter:source", "roll", "forward:阿英2", "exit:source"],
        )

    def test_owner_material_forward_never_calls_raw_message_directly(self):
        bot = WXBot.__new__(WXBot)
        events = []
        bot._ui_owner = object()
        bot._material_source_read_locks = {"素材源": RecordingLock("source", events)}
        bot._material_source_read_locks_guard = threading.Lock()
        bot._ui_forward_message = lambda _chat, _message, targets, **_kwargs: events.append(
            "owner:" + ",".join(targets)
        ) or True

        class RawMessage:
            type = "link"
            attr = "friend"
            sender = "素材源"
            content = "素材"

            def roll_into_view(self):
                raise AssertionError("owner 模式不得在业务线程操作原始消息")

            def forward(self, *_args, **_kwargs):
                raise AssertionError("owner 模式不得在业务线程直接转发")

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
