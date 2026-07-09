"""Runtime chat listener registry and single-chat takeover helpers."""


def normalize_chat_name(chat_who):
    return str(chat_who or "").strip()


def group_member_pause_key(group_who, sender):
    group_who = normalize_chat_name(group_who)
    sender = normalize_chat_name(sender)
    if not group_who or not sender:
        return ""
    return f"{group_who}::{sender}"


def ensure_pause_chat_reply_users(bot):
    if not hasattr(bot, "_pause_chat_reply_users") or bot._pause_chat_reply_users is None:
        bot._pause_chat_reply_users = set()
    return bot._pause_chat_reply_users


def pause_single_chat_reply(bot, chat_who):
    chat_who = normalize_chat_name(chat_who)
    if not chat_who:
        return False
    ensure_pause_chat_reply_users(bot).add(chat_who)
    return True


def resume_single_chat_reply(bot, chat_who):
    chat_who = normalize_chat_name(chat_who)
    if not chat_who:
        return False
    paused = ensure_pause_chat_reply_users(bot)
    if chat_who not in paused:
        return False
    paused.remove(chat_who)
    return True


def is_single_chat_reply_paused(bot, chat_who):
    chat_who = normalize_chat_name(chat_who)
    return bool(chat_who and chat_who in ensure_pause_chat_reply_users(bot))


def pause_message_reply(bot, chat_who, sender=None, chat_type=""):
    if str(chat_type or "").strip() == "group":
        key = group_member_pause_key(chat_who, sender)
        if not key:
            return False
        ensure_pause_chat_reply_users(bot).add(key)
        return True
    return pause_single_chat_reply(bot, chat_who)


def is_message_reply_paused(bot, chat_who, sender=None, chat_type=""):
    if str(chat_type or "").strip() == "group":
        key = group_member_pause_key(chat_who, sender)
        return bool(key and key in ensure_pause_chat_reply_users(bot))
    return is_single_chat_reply_paused(bot, chat_who)


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


def listen_chat_has_method(chat, method_name):
    return bool(chat and not isinstance(chat, dict) and callable(getattr(chat, method_name, None)))


def _acquire_wechat_action_lock(bot):
    getter = getattr(bot, "_get_wechat_action_lock", None)
    if not callable(getter):
        return None, True
    lock = getter()
    if lock is None:
        return None, True
    acquire = getattr(lock, "acquire", None)
    release = getattr(lock, "release", None)
    if not (callable(acquire) and callable(release)):
        return None, True
    if not acquire(blocking=False):
        return None, False
    return lock, True


def _release_wechat_action_lock(lock):
    if lock is not None:
        lock.release()


def remove_listen_chat(bot, name):
    if not hasattr(bot, "_listen_chats") or bot._listen_chats is None:
        bot._listen_chats = {}
    bot._listen_chats.pop(normalize_chat_name(name), None)


def send_text_to_target(bot, target, msg):
    lock, acquired = _acquire_wechat_action_lock(bot)
    if not acquired:
        sender = getattr(bot, "_send_text_to_target_without_child", None)
        if callable(sender):
            return sender(target, msg)
        return False
    try:
        chat = get_listen_chat(bot, target)
        if listen_chat_has_method(chat, "SendMsg"):
            with bot._get_chat_send_lock(target):
                result = chat.SendMsg(msg)
                remember_echo = getattr(bot, "_remember_private_outbound_echo_for_send_result", None)
                if callable(remember_echo):
                    remember_echo(target, result, "text", msg, source="runtime_send")
                return result
        verifier = getattr(bot, "_verified_send_chat", None)
        if callable(verifier):
            verified = verifier(target, chat)
            if verified:
                remember_listen_chat(bot, target, verified)
                with bot._get_chat_send_lock(target):
                    result = verified.SendMsg(msg)
                    remember_echo = getattr(bot, "_remember_private_outbound_echo_for_send_result", None)
                    if callable(remember_echo):
                        remember_echo(target, result, "text", msg, source="runtime_send")
                    return result
            if chat:
                remove_listen_chat(bot, target)
        sender = getattr(bot, "_send_text_to_target_without_child", None)
        if callable(sender):
            return sender(target, msg)
        return False
    finally:
        _release_wechat_action_lock(lock)


def send_file_to_target(bot, target, path):
    lock, acquired = _acquire_wechat_action_lock(bot)
    if not acquired:
        sender = getattr(bot, "_send_file_to_target_without_child", None)
        if callable(sender):
            return sender(target, path)
        return False
    try:
        chat = get_listen_chat(bot, target)
        if listen_chat_has_method(chat, "SendFiles"):
            with bot._get_chat_send_lock(target):
                result = chat.SendFiles(filepath=path)
                remember_echo = getattr(bot, "_remember_private_outbound_echo_for_send_result", None)
                if callable(remember_echo):
                    remember_echo(target, result, "file", source="runtime_send", path=path)
                return result
        verifier = getattr(bot, "_verified_send_chat", None)
        if callable(verifier):
            verified = verifier(target, chat)
            if verified:
                remember_listen_chat(bot, target, verified)
                with bot._get_chat_send_lock(target):
                    result = verified.SendFiles(filepath=path)
                    remember_echo = getattr(bot, "_remember_private_outbound_echo_for_send_result", None)
                    if callable(remember_echo):
                        remember_echo(target, result, "file", source="runtime_send", path=path)
                    return result
            if chat:
                remove_listen_chat(bot, target)
        sender = getattr(bot, "_send_file_to_target_without_child", None)
        if callable(sender):
            return sender(target, path)
        return False
    finally:
        _release_wechat_action_lock(lock)
