import unittest
from types import SimpleNamespace

from core.message_pipeline import ConversationRef, MessageEnvelope
from feature.message_routing import match_pending_voice_snapshot


class MessageEnvelopeTests(unittest.TestCase):
    def test_conversation_ref_normalizes_wxautox_friend_type(self):
        direct = ConversationRef(who="张三", chat_type="friend")
        callback = ConversationRef.from_wx_chat(
            SimpleNamespace(who="张三", chat_type="friend")
        )

        self.assertEqual(direct.chat_type, "private")
        self.assertEqual(callback.chat_type, "private")

    def test_conversation_ref_keeps_canonical_types_and_defaults_empty(self):
        self.assertEqual(ConversationRef("张三", "private").chat_type, "private")
        self.assertEqual(ConversationRef("群聊", "group").chat_type, "group")
        self.assertEqual(ConversationRef("张三", "").chat_type, "private")
        self.assertEqual(ConversationRef("张三", None).chat_type, "private")

    def test_conversation_ref_rejects_unknown_nonempty_type(self):
        with self.assertRaisesRegex(ValueError, "unsupported conversation chat_type"):
            ConversationRef("张三", "official")

    def test_callback_objects_are_copied_to_pure_records(self):
        chat = SimpleNamespace(who="张三", chat_type="private", SendMsg=lambda _text: None)
        message = SimpleNamespace(
            content="你好",
            type="text",
            sender="张三",
            attr="friend",
            id=12,
            hash="hash-1",
            hash_text="hash-text-1",
            time="10:20",
            download=lambda: "dangerous-path",
        )

        conversation = ConversationRef.from_wx_chat(chat)
        envelope = MessageEnvelope.from_wx_message(
            message,
            ingress_source="subwindow",
            received_at=123.5,
            window_order=2,
        )

        self.assertEqual(conversation, ConversationRef(who="张三", chat_type="private"))
        self.assertEqual(envelope.content, "你好")
        self.assertEqual(envelope.attr, "friend")
        self.assertEqual(envelope.id, 12)
        self.assertEqual(envelope.hash_text, "hash-text-1")
        self.assertEqual(envelope.window_order, 2)
        self.assertFalse(hasattr(conversation, "SendMsg"))
        self.assertFalse(hasattr(envelope, "download"))
        self.assertFalse(hasattr(envelope, "to_text"))

    def test_non_scalar_message_identity_is_converted_to_text(self):
        marker = SimpleNamespace(value="id")
        envelope = MessageEnvelope.from_wx_message(SimpleNamespace(id=marker))

        self.assertIsInstance(envelope.id, str)

    def test_same_hash_voice_messages_remain_pending_when_match_is_ambiguous(self):
        items = [
            {"key": "first", "signature": {"attr": "friend", "sender": "张三", "duration": 3, "hash": "same"}},
            {"key": "second", "signature": {"attr": "friend", "sender": "张三", "duration": 3, "hash": "same"}},
        ]
        messages = [
            MessageEnvelope(type="voice", attr="friend", sender="张三", content='语音3"秒第一条', hash="same"),
            MessageEnvelope(type="voice", attr="friend", sender="张三", content='语音3"秒第二条', hash="same"),
        ]

        matched = match_pending_voice_snapshot(items, messages)

        self.assertEqual(matched, {})

    def test_group_voice_matches_sender_before_duration(self):
        items = [
            {"key": "sender-b", "signature": {"attr": "friend", "sender": "B", "duration": 4, "hash": ""}},
        ]
        messages = [
            MessageEnvelope(type="voice", attr="friend", sender="A", content='语音4"秒A 的正文'),
            MessageEnvelope(type="voice", attr="friend", sender="B", content='语音4"秒B 的正文'),
        ]

        matched = match_pending_voice_snapshot(items, messages)

        self.assertEqual(matched["sender-b"].sender, "B")


if __name__ == "__main__":
    unittest.main()
