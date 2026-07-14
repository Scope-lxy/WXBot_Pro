"""Runtime registry for owner-backed listener handles."""

def normalize_chat_name(chat_who):
    return str(chat_who or "").strip()


def remember_listen_chat(bot, name, chat_obj):
    name = normalize_chat_name(name)
    if not name or not chat_obj or isinstance(chat_obj, dict):
        return
    if not hasattr(bot, "_listen_chats") or bot._listen_chats is None:
        bot._listen_chats = {}
    bot._listen_chats[name] = chat_obj


def get_listen_chat(bot, name):
    if not hasattr(bot, "_listen_chats") or bot._listen_chats is None:
        bot._listen_chats = {}
    return bot._listen_chats.get(normalize_chat_name(name))


def remove_listen_chat(bot, name):
    if not hasattr(bot, "_listen_chats") or bot._listen_chats is None:
        bot._listen_chats = {}
    bot._listen_chats.pop(normalize_chat_name(name), None)
