"""Runtime registry for owner-backed listener handles."""

from core.message_pipeline import ConversationRef

def normalize_chat_name(chat_who):
    return str(chat_who or "").strip()


def _conversation(name, chat_type="private"):
    if isinstance(name, ConversationRef):
        return name
    return ConversationRef(normalize_chat_name(name), chat_type)


def _registry(bot):
    if not hasattr(bot, "_listen_chats") or bot._listen_chats is None:
        bot._listen_chats = {}
    migrated = {}
    for key, value in bot._listen_chats.items():
        if isinstance(key, tuple) and len(key) == 2:
            migrated[key] = value
            continue
        conversation = ConversationRef.from_wx_chat(value)
        name = normalize_chat_name(key) or conversation.who
        if name:
            migrated[(conversation.chat_type, name)] = value
    if migrated != bot._listen_chats:
        bot._listen_chats = migrated
    return bot._listen_chats


def remember_listen_chat(bot, name, chat_obj, *, chat_type=None):
    actual = ConversationRef.from_wx_chat(chat_obj) if chat_obj else None
    conversation = _conversation(
        name,
        actual.chat_type if chat_type is None and actual is not None else chat_type or "private",
    )
    name = conversation.who
    if not name or not chat_obj or isinstance(chat_obj, dict):
        return
    if actual != conversation:
        raise ValueError("listener chat identity does not match the requested conversation")
    _registry(bot)[(conversation.chat_type, name)] = chat_obj


def get_listen_chat(bot, name, *, chat_type=None):
    registry = _registry(bot)
    normalized_name = normalize_chat_name(getattr(name, "who", name))
    if not normalized_name:
        return None
    if isinstance(name, ConversationRef) or chat_type is not None:
        conversation = _conversation(name, chat_type or "private")
        return registry.get((conversation.chat_type, conversation.who))
    matches = [
        value
        for (stored_type, stored_name), value in registry.items()
        if stored_type in {"private", "group"} and stored_name == normalized_name
    ]
    return matches[0] if len(matches) == 1 else None


def remove_listen_chat(bot, name, *, chat_type=None):
    registry = _registry(bot)
    normalized_name = normalize_chat_name(getattr(name, "who", name))
    if isinstance(name, ConversationRef) or chat_type is not None:
        conversation = _conversation(name, chat_type or "private")
        return registry.pop((conversation.chat_type, conversation.who), None) is not None
    keys = [key for key in registry if key[1] == normalized_name]
    for key in keys:
        registry.pop(key, None)
    return bool(keys)
