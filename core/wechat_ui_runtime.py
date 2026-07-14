"""Owner-thread wxautox adapter. No wxautox object leaves this class."""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from threading import RLock, get_ident
from typing import Any, Callable

from core.message_pipeline import (
    ConversationRef,
    MessageEnvelope,
    is_failed_voice_transcription_text,
    is_unrecognized_voice_placeholder,
    voice_message_body,
)
from core.wechat_ui_actions import (
    ActionBatchInterrupted,
    ContactBatchHandle,
    IntentNeedsExclusive,
    UI_CALL_WAIT_TIMEOUT,
    UIIntent,
    UIIntentKind,
)


class OwnedChat:
    """Compatibility facade containing only identity plus an owner reference."""

    def __init__(self, owner, who, chat_type="private"):
        self._owner = owner
        self.who = str(who or "").strip()
        self.chat_type = str(chat_type or "private").strip() or "private"

    def GetAllMessage(self):
        from core.wechat_ui_actions import UIIntent

        return self._owner.call(UIIntent(UIIntentKind.GET_MESSAGES, {"conversation": self.who}), UI_CALL_WAIT_TIMEOUT)

    def SendMsg(self, msg="", at=None, **kwargs):
        from core.wechat_ui_actions import UIIntent

        text = kwargs.get("message", msg)
        return self._owner.call(UIIntent(
            UIIntentKind.SEND_TEXT,
            {
                "conversation": self.who,
                "text": str(text or ""),
                "at": str(at or ""),
            },
            conversation_version=kwargs.get("conversation_version", 0),
        ), UI_CALL_WAIT_TIMEOUT)

    def SendMsgBatch(self, messages, *, conversation_version=0, first_at=""):
        from core.wechat_ui_actions import UIIntent

        return self._owner.call(UIIntent(
            UIIntentKind.SEND_TEXT_BATCH,
            {
                "conversation": self.who,
                "messages": [str(item or "") for item in (messages or []) if str(item or "")],
                "first_at": str(first_at or ""),
            },
            conversation_version=conversation_version,
        ), UI_CALL_WAIT_TIMEOUT)

    def SendActions(
        self,
        actions,
        *,
        conversation_version=0,
        task_key="",
        task_version=0,
        contact_key="",
        delivery_id="",
        require_contact_key=False,
    ):
        from core.wechat_ui_actions import UIIntent

        return self._owner.call(UIIntent(
            UIIntentKind.SEND_ACTIONS,
            {
                "conversation": self.who,
                "contact_key": str(contact_key or ""),
                "task_key": str(task_key or ""),
                "delivery_id": str(delivery_id or uuid.uuid4()),
                "require_contact_key": bool(require_contact_key),
                "actions": [dict(action or {}) for action in actions or []],
            },
            conversation_version=conversation_version,
            task_version=task_version,
        ), UI_CALL_WAIT_TIMEOUT)

    def SendFiles(self, filepath="", **kwargs):
        from core.wechat_ui_actions import UIIntent

        path = kwargs.get("path", filepath)
        return self._owner.call(UIIntent(UIIntentKind.SEND_FILE, {
            "conversation": self.who,
            "path": str(path or ""),
            "delivery_id": str(kwargs.get("delivery_id") or uuid.uuid4()),
        }), UI_CALL_WAIT_TIMEOUT)

    def SendAudio(self, filepath="", duration=None, **kwargs):
        from core.wechat_ui_actions import UIIntent

        path = kwargs.get("path", filepath)
        return self._owner.call(UIIntent(
            UIIntentKind.SEND_AUDIO,
            {
                "conversation": self.who,
                "path": str(path or ""),
                "delivery_id": str(kwargs.get("delivery_id") or uuid.uuid4()),
            },
            conversation_version=kwargs.get("conversation_version", 0),
        ), UI_CALL_WAIT_TIMEOUT)


class UIClientFacade:
    """Temporary API-compatible facade; it never stores a wxautox object."""

    is_ui_owner_facade = True

    def __init__(self, owner, identity):
        self._owner = owner
        self.nickname = str((identity or {}).get("nickname") or "")
        self._wx_id = str((identity or {}).get("wx_id") or self.nickname)

    def _main(self, operation, **payload):
        from core.wechat_ui_actions import UIIntent

        return self._owner.call(
            UIIntent(UIIntentKind.MAIN_WINDOW, {"operation": operation, **payload}),
            UI_CALL_WAIT_TIMEOUT,
        )

    def GetMyInfo(self):
        return {"id": self._wx_id, "nickname": self.nickname}

    def StartListening(self):
        return self._main("start_listening")

    def StopListening(self):
        return self._main("stop_listening")

    def IsOnline(self):
        return self._main("is_online")

    def SwitchToChat(self):
        return self._main("switch_to_chat")

    def GetSubWindow(self, nickname):
        name = str(nickname or "").strip()
        exists = self._main("has_subwindow", conversation=name)
        return OwnedChat(self._owner, name) if exists else None

    def GetAllSubWindow(self):
        return [OwnedChat(self._owner, name) for name in self._main("all_subwindows")]

    def AddListenChat(self, nickname, callback=None):
        from core.wechat_ui_actions import UIIntent

        name = str(nickname or "").strip()
        self._owner.call(UIIntent(UIIntentKind.ADD_LISTEN, {"conversation": name}), UI_CALL_WAIT_TIMEOUT)
        return OwnedChat(self._owner, name)

    def RemoveListenChat(self, nickname=None, who=None):
        from core.wechat_ui_actions import UIIntent

        name = str(nickname or who or "").strip()
        return self._owner.call(UIIntent(UIIntentKind.REMOVE_LISTEN, {"conversation": name}), UI_CALL_WAIT_TIMEOUT)

    def GetListenMessage(self):
        from core.wechat_ui_actions import UIIntent

        self._owner.call(UIIntent(UIIntentKind.POLL_MESSAGES, {"mode": "listen"}), UI_CALL_WAIT_TIMEOUT)
        return {}

    def GetNextNewMessage(self, filter_mute=False, callback=None, download_media=False):
        from core.wechat_ui_actions import UIIntent

        return self._owner.call(UIIntent(UIIntentKind.POLL_MESSAGES, {
            "mode": "next",
            "filter_mute": bool(filter_mute),
            "download_media": bool(download_media),
        }), UI_CALL_WAIT_TIMEOUT)

    def ChatWith(self, who=None, exact=True):
        name = str(who or "").strip()
        self._main("chat_with", conversation=name, exact=bool(exact))
        return OwnedChat(self._owner, name)

    def SendMsg(self, who=None, msg="", **kwargs):
        return OwnedChat(self._owner, who).SendMsg(msg=msg, at=kwargs.get("at"))

    def SendFiles(self, who=None, filepath="", **kwargs):
        return OwnedChat(self._owner, who).SendFiles(filepath=filepath)


class WeChatUIRuntime:
    def __init__(
        self,
        on_message: Callable[[ConversationRef, MessageEnvelope], None],
        client_factory=None,
        inbound_media_enabled=None,
    ):
        self._on_message = on_message
        self._client_factory = client_factory or self._default_client_factory
        self._inbound_media_enabled = inbound_media_enabled or (lambda _conversation, _message_type: False)
        self._client = None
        self._owner = None
        self._listen_chats = {}
        self._heartbeat = lambda: None
        self._callback_suppression_lock = RLock()
        self._callback_suppression = {}

    def set_heartbeat(self, heartbeat):
        self._heartbeat = heartbeat if callable(heartbeat) else (lambda: None)

    def set_owner(self, owner):
        self._owner = owner

    @staticmethod
    def _default_client_factory(version):
        from wxautox4 import WeChat

        return WeChat(version=version)

    def handlers(self):
        return {
            UIIntentKind.BOOTSTRAP: self.bootstrap,
            UIIntentKind.REBIND: self.rebind,
            UIIntentKind.SHUTDOWN: self.shutdown,
            UIIntentKind.POLL_MESSAGES: self.poll_messages,
            UIIntentKind.GET_MESSAGES: self.get_messages,
            UIIntentKind.SEND_TEXT: self.send_text,
            UIIntentKind.SEND_TEXT_BATCH: self.send_text_batch,
            UIIntentKind.SEND_ACTIONS: self.send_actions,
            UIIntentKind.SEND_FILE: self.send_file,
            UIIntentKind.SEND_AUDIO: self.send_audio,
            UIIntentKind.ADD_LISTEN: self.add_listen,
            UIIntentKind.REMOVE_LISTEN: self.remove_listen,
            UIIntentKind.CONTACT_RECOVER: self.recover_chat_page,
            UIIntentKind.MAIN_WINDOW: self.main_window,
            UIIntentKind.DOWNLOAD_MEDIA: self.download_media,
            UIIntentKind.FORWARD: self.forward_message,
            UIIntentKind.QUOTE: self.quote_message,
            UIIntentKind.MATERIAL_READ: self.read_material_messages,
            UIIntentKind.NEW_FRIEND: self.process_new_friends,
            UIIntentKind.CONTACT_EDIT: self.edit_contact,
            UIIntentKind.RELATIONSHIP_SCAN: self.scan_relationship_sessions,
            UIIntentKind.FRIEND_REQUEST: self.send_friend_request,
            UIIntentKind.CONTACT_START: self.start_contact_batch,
        }

    def _callback(self, message, chat):
        conversation = ConversationRef.from_wx_chat(chat)
        suppression_key = (conversation.who, get_ident())
        with self._callback_suppression_lock:
            if self._callback_suppression.get(suppression_key, 0) > 0:
                return True
        envelope = MessageEnvelope.from_wx_message(
            message,
            ingress_source="subwindow",
            received_at=time.time(),
        )
        envelope._wxbot_source_batch = f"subwindow:{time.time_ns()}"
        if (
            envelope.type in {"image", "quote"}
            and self._inbound_media_enabled(conversation, envelope.type)
        ):
            method_name = "download_quote_image" if envelope.type == "quote" else "download"
            method = getattr(message, method_name, None)
            try:
                owner = self._owner
                path = str(owner.run_callback_action(
                    UIIntent(UIIntentKind.DOWNLOAD_MEDIA, {
                        "conversation": conversation.who,
                        "callback_bound_message": True,
                    }),
                    method,
                ) or "") if owner is not None and callable(method) else ""
            except Exception:
                path = ""
            if path:
                envelope.content = (
                    envelope.content + "+引用的图片:" + path
                    if envelope.type == "quote"
                    else path
                )
                envelope._wxbot_media_prepared = True
            elif envelope.type == "image":
                envelope._wxbot_media_prepared = True
                envelope._skip_ai_reply = True
                envelope._skip_memory = True
        self._on_message(conversation, envelope)
        return True

    @contextmanager
    def _suppress_callbacks_for(self, conversation):
        name = str(conversation or "").strip()
        if not name:
            yield
            return
        suppression_key = (name, get_ident())
        with self._callback_suppression_lock:
            self._callback_suppression[suppression_key] = self._callback_suppression.get(suppression_key, 0) + 1
        try:
            yield
        finally:
            with self._callback_suppression_lock:
                remaining = self._callback_suppression.get(suppression_key, 0) - 1
                if remaining > 0:
                    self._callback_suppression[suppression_key] = remaining
                else:
                    self._callback_suppression.pop(suppression_key, None)

    @staticmethod
    def _snapshot_envelope(message, *, ingress_source, window_order):
        envelope = MessageEnvelope.from_wx_message(
            message,
            ingress_source=ingress_source,
            window_order=window_order,
        )
        if envelope.type != "voice" or voice_message_body(envelope.content):
            return envelope
        try:
            visible_text = str(getattr(getattr(message, "control", None), "Name", "") or "").strip()
        except Exception:
            return envelope
        if (
            not voice_message_body(visible_text)
            or is_failed_voice_transcription_text(visible_text)
            or is_unrecognized_voice_placeholder(visible_text)
        ):
            return envelope
        envelope.content = visible_text
        envelope.original_content = visible_text
        return envelope

    def _create_client(self):
        last_error = None
        for version in ("微信", "WeChat"):
            try:
                return self._client_factory(version)
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("未能初始化微信客户端")

    @staticmethod
    def _verified_chat(chat, name):
        return chat if chat is not None and str(getattr(chat, "who", "") or "").strip() == name else None

    def _find_chat(self, name):
        name = str(name or "").strip()
        cached = self._verified_chat(self._listen_chats.get(name), name)
        if cached is not None:
            return cached
        self._listen_chats.pop(name, None)
        getter = getattr(self._client, "GetSubWindow", None)
        if callable(getter):
            chat = self._verified_chat(getter(nickname=name), name)
            if chat is not None:
                self._listen_chats[name] = chat
                return chat
        return None

    def _add_chat(self, name):
        add = getattr(self._client, "AddListenChat", None)
        if not callable(add):
            raise RuntimeError("当前微信内核不支持添加监听")
        chat = self._verified_chat(add(nickname=name, callback=self._callback), name)
        if chat is None:
            chat = self._find_chat(name)
        if chat is None:
            raise RuntimeError(f"未能建立监听子窗口：{name}")
        self._listen_chats[name] = chat
        return chat

    def _chat_for_payload(self, payload, *, allow_add):
        if self._client is None:
            raise RuntimeError("微信 UI owner 尚未初始化")
        name = str(payload.get("conversation") or "").strip()
        if not name:
            raise ValueError("微信 UI 意图缺少会话名称")
        chat = self._find_chat(name)
        if chat is not None:
            return chat
        if not allow_add:
            raise IntentNeedsExclusive()
        return self._add_chat(name)

    def bootstrap(self, payload):
        self._client = self._create_client()
        self._listen_chats = {}
        nickname = str(getattr(self._client, "nickname", "") or "")
        info = {}
        get_info = getattr(self._client, "GetMyInfo", None)
        if callable(get_info):
            info = dict(get_info() or {})
        stop = getattr(self._client, "StopListening", None)
        if callable(stop):
            stop()
        start = getattr(self._client, "StartListening", None)
        if callable(start):
            start()
        registered = []
        for raw in payload.get("listeners") or ():
            name = str(raw.get("name") if isinstance(raw, Mapping) else raw or "").strip()
            if not name or name in registered:
                continue
            self._add_chat(name)
            registered.append(name)
        return {
            "nickname": nickname,
            "wx_id": str(info.get("id") or nickname),
            "listeners": registered,
        }

    def rebind(self, payload):
        listeners = []
        for raw in payload.get("listeners") or tuple(self._listen_chats):
            name = str(raw.get("name") if isinstance(raw, Mapping) else raw or "").strip()
            if name and name not in listeners:
                listeners.append(name)
        old_client = self._client
        if old_client is not None:
            stop = getattr(old_client, "StopListening", None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    pass
        self._client = None
        self._listen_chats = {}
        return self.bootstrap({"listeners": listeners})

    def shutdown(self, _payload):
        stop = getattr(self._client, "StopListening", None)
        if callable(stop):
            stop()
        self._listen_chats = {}
        self._client = None
        return True

    def add_listen(self, payload):
        return bool(self._add_chat(str(payload.get("conversation") or "").strip()))

    def remove_listen(self, payload):
        name = str(payload.get("conversation") or "").strip()
        remove = getattr(self._client, "RemoveListenChat", None)
        result = remove(nickname=name) if callable(remove) else None
        self._listen_chats.pop(name, None)
        return result

    def get_messages(self, payload):
        chat = self._chat_for_payload(payload, allow_add=bool(payload.get("_exclusive_retry")))
        with self._suppress_callbacks_for(getattr(chat, "who", "")):
            messages = list(chat.GetAllMessage() or [])
        return [
            self._snapshot_envelope(message, ingress_source="window_snapshot", window_order=index)
            for index, message in enumerate(messages)
        ]

    def send_text(self, payload):
        chat = self._chat_for_payload(payload, allow_add=bool(payload.get("_exclusive_retry")))
        kwargs = {"msg": str(payload.get("text") or "")}
        at = payload.get("at")
        if at:
            kwargs["at"] = at
        try:
            return chat.SendMsg(**kwargs)
        except TypeError:
            return chat.SendMsg(kwargs["msg"])

    def send_text_batch(self, payload):
        chat = self._chat_for_payload(payload, allow_add=bool(payload.get("_exclusive_retry")))
        results = []
        first_at = str(payload.get("first_at") or "")
        for index, text in enumerate(payload.get("messages") or ()):
            if index == 0 and first_at:
                try:
                    results.append(chat.SendMsg(msg=str(text or ""), at=first_at))
                except TypeError:
                    results.append(chat.SendMsg(str(text or ""), at=first_at))
            else:
                results.append(chat.SendMsg(str(text or "")))
        return results

    def send_actions(self, payload):
        chat = self._chat_for_payload(payload, allow_add=True)
        results = []
        for index, action in enumerate(payload.get("actions") or ()):
            action = dict(action or {})
            kind = str(action.get("type") or "text").strip().lower()
            try:
                if kind == "file":
                    results.append(chat.SendFiles(filepath=str(action.get("path") or "")))
                elif kind in {"voice", "audio"}:
                    path = str(action.get("path") or "")
                    results.append(chat.SendAudio(filepath=path, duration=None))
                else:
                    text = str(action.get("text") or action.get("content") or "")
                    at = str(action.get("at") or "")
                    if at:
                        results.append(chat.SendMsg(msg=text, at=at))
                    else:
                        results.append(chat.SendMsg(text))
            except Exception as exc:
                raise ActionBatchInterrupted(results, index, exc) from exc
        return results

    def send_file(self, payload):
        chat = self._chat_for_payload(payload, allow_add=True)
        return chat.SendFiles(filepath=str(payload.get("path") or ""))

    def send_audio(self, payload):
        chat = self._chat_for_payload(payload, allow_add=True)
        path = str(payload.get("path") or "")
        return chat.SendAudio(filepath=path, duration=None)

    def poll_messages(self, payload):
        if self._client is None:
            return []
        mode = str(payload.get("mode") or "next")
        if mode == "listen":
            getter = getattr(self._client, "GetListenMessage", None)
            result = getter() if callable(getter) else {}
            for chat, messages in (result or {}).items():
                for message in messages or []:
                    self._callback(message, chat)
            return len(result or {})
        getter = getattr(self._client, "GetNextNewMessage", None)
        if not callable(getter):
            return {"chat_name": "", "chat_type": "", "msg": []}
        captured = []

        def capture(message):
            captured.append(message)
            return True

        result = getter(filter_mute=bool(payload.get("filter_mute")), callback=capture)
        result = result if isinstance(result, dict) else {}
        chat_name = str(result.get("chat_name") or "").strip()
        chat_type = str(result.get("chat_type") or getattr(self._client, "chat_type", "private") or "private")
        raw_messages = list(result.get("msg") or captured)
        envelopes = []
        source_batch = f"global:{time.time_ns()}"
        for index, message in enumerate(raw_messages):
            envelope = MessageEnvelope.from_wx_message(
                message,
                ingress_source="global",
                received_at=time.time(),
                window_order=index,
            )
            envelope._wxbot_source_batch = source_batch
            if payload.get("download_media") and chat_type != "group":
                method_name = "download_quote_image" if envelope.type == "quote" else "download"
                if envelope.type in {"image", "quote"}:
                    method = getattr(message, method_name, None)
                    path = str(method() or "") if callable(method) else ""
                    if path:
                        envelope.content = (
                            envelope.content + "+引用的图片:" + path
                            if envelope.type == "quote"
                            else path
                        )
                        envelope._wxbot_media_prepared = True
            envelopes.append(envelope)
        return {"chat_name": chat_name, "chat_type": chat_type, "msg": envelopes}

    def main_window(self, payload):
        operation = str(payload.get("operation") or "")
        if operation == "start_listening":
            return self._client.StartListening()
        if operation == "stop_listening":
            return self._client.StopListening()
        if operation == "is_online":
            return self._client.IsOnline()
        if operation == "switch_to_chat":
            switch = getattr(self._client, "SwitchToChat", None)
            return switch() if callable(switch) else True
        if operation == "has_subwindow":
            return self._find_chat(str(payload.get("conversation") or "")) is not None
        if operation == "all_subwindows":
            getter = getattr(self._client, "GetAllSubWindow", None)
            chats = list(getter() or []) if callable(getter) else list(self._listen_chats.values())
            names = []
            current = {}
            for chat in chats:
                name = str(getattr(chat, "who", "") or "").strip()
                if name and name not in names:
                    current[name] = chat
                    names.append(name)
            self._listen_chats = current
            return names
        if operation == "chat_with":
            name = str(payload.get("conversation") or "").strip()
            chat_with = getattr(self._client, "ChatWith", None)
            if not callable(chat_with):
                raise RuntimeError("当前微信内核不支持 ChatWith")
            return bool(chat_with(who=name, exact=bool(payload.get("exact", True))))
        raise ValueError(f"未登记的主窗口操作：{operation}")

    def _locate_message(self, payload):
        chat = self._chat_for_payload(payload, allow_add=True)
        with self._suppress_callbacks_for(getattr(chat, "who", "")):
            messages = list(chat.GetAllMessage() or [])
            located = self._locate_message_in_snapshot(messages, payload, allow_window_order=True)
            if located is not None:
                return located

            if not bool(payload.get("allow_history_fallback", True)):
                raise RuntimeError("当前可见窗口未找到原消息，已停止历史翻页定位")

            history_reader = getattr(chat, "GetHistoryMessage", None)
            if not callable(history_reader):
                chat_with = getattr(self._client, "ChatWith", None)
                history_reader = getattr(self._client, "GetHistoryMessage", None)
                if callable(chat_with) and callable(history_reader):
                    chat_with(who=str(payload.get("conversation") or ""), exact=True)
            if callable(history_reader):
                history = list(history_reader(50, interval=0.2, speed=5, goback=True) or [])
                located = self._locate_message_in_snapshot(history, payload, allow_window_order=False)
                if located is not None:
                    return located
        raise RuntimeError("当前窗口和近期历史均未找到原消息")

    @staticmethod
    def _locate_message_in_snapshot(messages, payload, *, allow_window_order):
        wanted_type = str(payload.get("message_type") or "")
        wanted_attr = str(payload.get("message_attr") or "")
        wanted_sender = str(payload.get("message_sender") or "")
        wanted_content = str(payload.get("message_content") or "")
        wanted_id = payload.get("message_id")
        wanted_hash = payload.get("message_hash")
        wanted_hash_text = payload.get("message_hash_text")
        candidates = []
        for index, message in enumerate(messages):
            if wanted_type and str(getattr(message, "type", "") or "") != wanted_type:
                continue
            if wanted_attr and str(getattr(message, "attr", "") or "") != wanted_attr:
                continue
            if wanted_sender and str(getattr(message, "sender", "") or "") != wanted_sender:
                continue
            if wanted_content and str(getattr(message, "content", "") or "") != wanted_content:
                continue
            candidates.append((index, message))
        if wanted_id not in {None, ""}:
            narrowed = [item for item in candidates if getattr(item[1], "id", "") == wanted_id]
            if narrowed:
                candidates = narrowed
        if wanted_hash not in {None, ""}:
            narrowed = [item for item in candidates if getattr(item[1], "hash", None) == wanted_hash]
            if narrowed:
                candidates = narrowed
        if wanted_hash_text not in {None, ""}:
            narrowed = [item for item in candidates if getattr(item[1], "hash_text", None) == wanted_hash_text]
            if narrowed:
                candidates = narrowed
        if not candidates:
            return None
        if allow_window_order and payload.get("message_window_order_known"):
            wanted_order = max(0, int(payload.get("message_window_order") or 0))
            exact = [message for index, message in candidates if index == wanted_order]
            if len(exact) == 1:
                return exact[0]
        if len(candidates) == 1:
            return candidates[0][1]
        raise RuntimeError("当前窗口存在多条相同消息，已拒绝猜测原消息")

    def download_media(self, payload):
        message = self._locate_message(payload)
        method_name = "download_quote_image" if payload.get("quote_image") else "download"
        method = getattr(message, method_name, None)
        return str(method() or "") if callable(method) else ""

    def forward_message(self, payload):
        message = self._locate_message(payload)
        targets = payload.get("targets") or payload.get("target") or ""
        preface = str(payload.get("preface") or "")
        roll_into_view = getattr(message, "roll_into_view", None)
        if callable(roll_into_view):
            roll_into_view()
        if preface:
            return message.forward(targets, message=preface)
        return message.forward(targets)

    def quote_message(self, payload):
        message = self._locate_message(payload)
        roll_into_view = getattr(message, "roll_into_view", None)
        if callable(roll_into_view):
            roll_into_view()
        return message.quote(str(payload.get("text") or ""), at=str(payload.get("at") or "") or None)

    def read_material_messages(self, payload):
        from core.logger import log
        from feature.material_outreach import build_stable_material_signature, is_forwardable_material_message
        from wxautox4.param import WxParam

        source = str(payload.get("conversation") or "").strip()
        limit = max(1, int(payload.get("limit") or 1))
        target_signature = str(payload.get("target_signature") or "").strip()
        goback = bool(payload.get("goback", True))
        require_forwardable = bool(payload.get("require_forwardable", True))
        with self._suppress_callbacks_for(source):
            chat = self._chat_for_payload({"conversation": source}, allow_add=True)
        readers = []
        chat_box = getattr(chat, "ChatBox", None)
        internal = getattr(chat_box, "get_msgs_from_history", None)
        public = getattr(chat, "GetHistoryMessage", None)
        if callable(internal):
            readers.append((internal, "子窗口内部 ChatBox.get_msgs_from_history"))
        if callable(public):
            readers.append((public, "子窗口公开 GetHistoryMessage"))

        last_messages = None
        last_strategy = "未读取到可转发素材"

        def read_history(reader):
            forwardable_seen = 0
            stop_sign = getattr(WxParam, "CALLBACK_STOP_SIGN", "stop")

            def stop_after_enough(message):
                nonlocal forwardable_seen
                if target_signature and build_stable_material_signature(message) == target_signature:
                    return stop_sign
                if is_forwardable_material_message(message):
                    forwardable_seen += 1
                if forwardable_seen >= limit:
                    return stop_sign
                return None

            with self._suppress_callbacks_for(source):
                return list(reader(
                    limit,
                    callback=stop_after_enough,
                    interval=0.2,
                    speed=5,
                    goback=goback,
                ) or [])

        def messages_are_usable(messages):
            return not require_forwardable or any(is_forwardable_material_message(message) for message in messages)

        for reader, strategy in readers:
            try:
                messages = read_history(reader)
                last_messages = messages
                last_strategy = strategy
                if messages_are_usable(messages):
                    break
                log(
                    level="WARNING",
                    message=(
                        f"[素材转发] 读取素材历史未发现可转发素材，准备尝试下一读取方案："
                        f"来源 {source}，方案 {strategy}，读取 {len(messages)} 条"
                    ),
                )
            except Exception as exc:
                log(
                    level="WARNING",
                    message=(
                        f"[素材转发] 子窗口读取素材历史失败，准备尝试下一读取方案："
                        f"来源 {source}，方案 {strategy}，{exc}"
                    ),
                )
        else:
            messages = None

        if last_messages is None or not messages_are_usable(last_messages):
            chat_with = getattr(self._client, "ChatWith", None)
            main_history = getattr(self._client, "GetHistoryMessage", None)
            if callable(chat_with) and callable(main_history):
                strategy = "主窗口公开 GetHistoryMessage"
                try:
                    with self._suppress_callbacks_for(source):
                        chat_with(who=source, exact=True)
                    messages = read_history(main_history)
                    last_messages = messages
                    last_strategy = strategy
                    if not messages_are_usable(messages):
                        log(
                            level="WARNING",
                            message=(
                                f"[素材转发] 读取素材历史未发现可转发素材，准备尝试下一读取方案："
                                f"来源 {source}，方案 {strategy}，读取 {len(messages)} 条"
                            ),
                        )
                except Exception as exc:
                    log(
                        level="WARNING",
                        message=(
                            f"[素材转发] 主窗口读取素材历史失败，准备尝试子窗口可见消息兜底："
                            f"来源 {source}，{exc}"
                        ),
                    )

        if last_messages is None or not messages_are_usable(last_messages):
            getter = getattr(chat, "GetAllMessage", None)
            if callable(getter):
                with self._suppress_callbacks_for(source):
                    last_messages = list(getter() or [])[-limit:]
                last_strategy = "子窗口可见 GetAllMessage"
            elif last_messages is None:
                raise RuntimeError("素材来源窗口不支持读取消息")

        envelopes = [
            MessageEnvelope.from_wx_message(message, ingress_source="material_history", window_order=index)
            for index, message in enumerate(last_messages)
        ]
        return {"messages": envelopes, "strategy": last_strategy}

    def process_new_friends(self, payload):
        from feature.new_friends import build_new_friend_remark

        getter = getattr(self._client, "GetNewFriends", None)
        candidates = list(getter(acceptable=True) or []) if callable(getter) else []
        identities = [self._new_friend_identity(candidate) for candidate in candidates]
        accepted = []
        for identity in identities:
            if identities.count(identity) != 1:
                continue
            refreshed = list(getter(acceptable=True) or [])
            matches = [
                candidate for candidate in refreshed
                if self._new_friend_identity(candidate) == identity
            ]
            if len(matches) != 1:
                continue
            candidate = matches[0]
            name = identity[0]
            if not name:
                continue
            accept_kwargs = {}
            rules = dict(payload.get("remark_rules") or {})
            remark = ""
            if rules.get("enabled"):
                remark = build_new_friend_remark(
                    name,
                    prefix=str(rules.get("prefix") or ""),
                    suffix=str(rules.get("suffix") or ""),
                    prefix_timestamp=bool(rules.get("prefix_timestamp")),
                    suffix_timestamp=bool(rules.get("suffix_timestamp")),
                )
            if remark:
                accept_kwargs["remark"] = remark
            tags = list(payload.get("tags") or [])
            if tags:
                accept_kwargs["tags"] = tags
            candidate.accept(**accept_kwargs)
            send_name = remark or name
            accepted.append({
                "name": name,
                "send_name": send_name,
                "remark": remark,
                "tags": tags,
            })
            break
        switch = getattr(self._client, "SwitchToChat", None)
        if callable(switch):
            switch()
        return accepted

    @staticmethod
    def _new_friend_identity(candidate):
        return (
            str(getattr(candidate, "name", "") or "").strip(),
            str(getattr(candidate, "content", "") or "").strip(),
            bool(getattr(candidate, "acceptable", True)),
        )

    def edit_contact(self, payload):
        from core.wechat_window import (
            bring_wechat_main_window_to_front,
            move_cursor_to_wechat_main_window_center,
        )
        from feature.contacts import (
            _chat_info_tags,
            _tags_update_is_noop,
            friend_info_edit_noop,
            friend_info_edit_success,
        )

        target = str(payload.get("target") or "").strip()
        if not target:
            raise ValueError("好友资料编辑缺少目标名称")
        chat_with = getattr(self._client, "ChatWith", None)
        if not callable(chat_with):
            raise RuntimeError("当前微信内核不支持打开好友聊天窗口")
        bring_wechat_main_window_to_front(wait=0.3)
        chat_with(who=target, exact=True)
        bring_wechat_main_window_to_front(wait=0.3)
        move_cursor_to_wechat_main_window_center(wait=0.05)

        chat_info_getter = getattr(self._client, "ChatInfo", None)
        chat_info = dict(chat_info_getter() or {}) if callable(chat_info_getter) else {}
        chat_type = str(chat_info.get("chat_type") or "").strip()
        chat_name = str(chat_info.get("chat_name") or "").strip()
        expected_names = {str(name or "").strip() for name in payload.get("expected_names") or ()}
        expected_names.add(target)
        if chat_type and chat_type != "friend":
            raise RuntimeError(f"当前会话不是好友会话：{chat_type}")
        if chat_name and chat_name not in expected_names:
            raise RuntimeError(f"当前会话不是目标好友：{chat_name}")

        add_tags = list(payload.get("add_tags") or [])
        remove_tags = list(payload.get("remove_tags") or [])
        if payload.get("remark") is None and _tags_update_is_noop(
            _chat_info_tags(chat_info),
            add_tags=add_tags,
            remove_tags=remove_tags,
        ):
            return {
                "status": "成功",
                "message": "标签已满足要求，未进行任何修改",
                "noop": True,
            }

        editor = getattr(self._client, "EditFriendInfo", None)
        if not callable(editor):
            raise RuntimeError("当前微信内核不支持修改好友资料")
        bring_wechat_main_window_to_front(wait=0.3)
        move_cursor_to_wechat_main_window_center(wait=0.05)
        response = editor(
            remark=payload.get("remark"),
            add_tags=add_tags,
            remove_tags=remove_tags,
            tag_wait=0.8,
        )
        if friend_info_edit_noop(response):
            response = dict(response)
            response["status"] = "成功"
            response["noop"] = True
        elif not friend_info_edit_success(response):
            raise RuntimeError(f"修改好友信息未返回明确成功：{response}")
        return response

    def scan_relationship_sessions(self, payload):
        from feature.relationship_scan import normalize_session_items

        mode = str(payload.get("mode") or "current")
        getter = getattr(self._client, "GetSession", None)
        if not callable(getter):
            return []
        if mode == "current":
            return normalize_session_items(getter())
        if mode != "full":
            raise ValueError(f"未登记的关系扫描模式：{mode}")
        session_box = getattr(self._client, "SessionBox", None)
        go_top = getattr(session_box, "go_top", None)
        roll_down = getattr(session_box, "roll_down", None)
        sessions_by_name = {}
        stale_rounds = 0
        scrolls = 0
        max_scrolls = max(1, int(payload.get("max_scrolls") or 1))
        stale_limit = max(1, int(payload.get("stale_rounds") or 8))
        can_scroll = callable(roll_down)
        try:
            if callable(go_top):
                go_top()
                self._heartbeat()
                time.sleep(1.0)
            while scrolls < max_scrolls and stale_rounds < stale_limit:
                batch = normalize_session_items(getter())
                self._heartbeat()
                before_count = len(sessions_by_name)
                for session in batch:
                    name = str(session.get("name") or "").strip()
                    if name and name not in sessions_by_name:
                        sessions_by_name[name] = session
                stale_rounds = stale_rounds + 1 if len(sessions_by_name) == before_count else 0
                if stale_rounds >= stale_limit or not can_scroll:
                    break
                roll_down()
                scrolls += 1
                self._heartbeat()
        finally:
            if callable(go_top):
                try:
                    go_top()
                    self._heartbeat()
                    time.sleep(1.0)
                except Exception as exc:
                    from core.logger import log

                    log(level="WARNING", message=f"[关系扫描] 扫描结束后会话列表回顶失败：{exc}")
        hit_safety_limit = scrolls >= max_scrolls and stale_rounds < stale_limit and can_scroll
        return {
            "sessions": list(sessions_by_name.values()),
            "scrolls": scrolls,
            "hit_safety_limit": hit_safety_limit,
        }

    def send_friend_request(self, payload):
        from types import SimpleNamespace
        from feature.friend_request_senders import ConversationVerifySender

        sender = ConversationVerifySender(assert_owner_thread=self._heartbeat)
        target = str(payload.get("target") or "")
        with self._suppress_callbacks_for(target):
            return sender.send(
                SimpleNamespace(wx=self._client),
                target,
                addmsg=str(payload.get("addmsg") or ""),
                remark=str(payload.get("remark") or ""),
                tags=list(payload.get("tags") or []),
                permission=str(payload.get("permission") or "不设置"),
                max_attempts=max(1, int(payload.get("max_attempts") or 2)),
            )

    def start_contact_batch(self, payload):
        from feature.contacts import start_contact_auto_maintenance_collector

        process = start_contact_auto_maintenance_collector(
            start_name=str(payload.get("start_name") or ""),
            start_identity=str(payload.get("start_identity") or ""),
            count=50,
            timeout_seconds=300,
        )
        return ContactBatchHandle(poll=process.poll, terminate=process.terminate)

    def recover_chat_page(self, _payload):
        switch = getattr(self._client, "SwitchToChat", None)
        return switch() if callable(switch) else True
