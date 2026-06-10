"""Custom forwarding business rules."""

FORWARD_MESSAGE_TYPES = {
    "image",
    "video",
    "file",
    "location",
    "link",
    "emotion",
    "merge",
    "personal_card",
    "note",
    "miniapp",
}


def custom_forward_rule_enabled(rule):
    """Return whether a custom forwarding rule is enabled."""
    return isinstance(rule, dict) and bool(rule.get("enabled", False))


def iter_custom_forward_listen_sources(rules, *, listen_list, groups, group_switch, command_chat):
    """
    Yield extra source chats that must be listened for custom forwarding.

    Sources already covered by normal private/group listeners are skipped. When
    group listening is off, configured groups are not considered already listened.
    """
    listened_groups = set(groups or []) if group_switch else set()
    already_listened = set(listen_list or []) | listened_groups | {command_chat}
    seen = set()
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        if not custom_forward_rule_enabled(rule):
            continue
        for source in rule.get("sources", []):
            if not source or source in already_listened or source in seen:
                continue
            seen.add(source)
            yield source


def is_custom_forward_source(rules, chat_who):
    """Return whether a chat is an explicit source of any custom forward rule."""
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        if not custom_forward_rule_enabled(rule):
            continue
        if chat_who in rule.get("sources", []):
            return True
    return False


def _is_group_chat(chat_who, *, group_chats=None, chat_type=""):
    return str(chat_type or "").strip() == "group" or chat_who in set(group_chats or [])


def custom_forward_rule_matches(rule, chat_who, message, *, group_chats=None, chat_type=""):
    """Return whether a custom forwarding rule matches the incoming message."""
    if not custom_forward_rule_enabled(rule):
        return False
    sources = [str(source or "").strip() for source in rule.get("sources", []) if str(source or "").strip()]
    is_group = _is_group_chat(chat_who, group_chats=group_chats, chat_type=chat_type)
    if sources:
        if chat_who not in sources:
            return False
    elif is_group and rule.get("type", "all") == "all" and not rule.get("forward_group_friend_messages", False):
        return False

    rule_type = rule.get("type", "all")
    if rule_type == "all":
        return True
    if rule_type == "keyword":
        content = getattr(message, "content", "")
        return any(keyword and keyword in content for keyword in rule.get("keywords", []))
    return False


def _source_message(chat_who, message):
    return f"发送人：{getattr(message, 'sender', '')}（窗口：{chat_who}）"


def _iter_forward_targets(rule, default_target=""):
    targets = [str(target or "").strip() for target in rule.get("targets", [])]
    if not targets:
        targets = [str(default_target or "").strip()]
    seen = set()
    for target in targets:
        if not target:
            target = str(default_target or "").strip()
        if not target or target in seen:
            continue
        seen.add(target)
        yield target


def iter_custom_forward_actions(rules, chat_who, message, *, group_chats=None, chat_type="", default_target=""):
    """Yield executable forwarding actions for all rules matching a message."""
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        if not custom_forward_rule_matches(rule, chat_who, message, group_chats=group_chats, chat_type=chat_type):
            continue

        rule_type = rule.get("type", "all")
        source_message = _source_message(chat_who, message) if rule.get("forward_with_source", False) else None
        message_type = getattr(message, "type", "")
        is_forward = message_type in FORWARD_MESSAGE_TYPES
        for target in _iter_forward_targets(rule, default_target=default_target):
            content = None
            if not is_forward:
                content = getattr(message, "content", "")
                if source_message:
                    content = content + "\n\n" + source_message
            yield {
                "rule_type": rule_type,
                "target": target,
                "kind": "forward" if is_forward else "text",
                "content": content,
                "source_message": source_message,
            }


def plan_custom_forward_takeover(rules, chat_who, message, *, group_chats=None, chat_type="", default_target=""):
    """Return takeover decision and matching actions for custom forwarding."""
    matched_rules = []
    should_pause = False
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        if custom_forward_rule_matches(rule, chat_who, message, group_chats=group_chats, chat_type=chat_type):
            matched_rules.append(rule)
            if rule.get("type", "all") == "keyword" and rule.get("pause_ai_reply_on_match", False):
                should_pause = True

    if not should_pause:
        return {"should_takeover": False, "should_pause": False, "pause_chat": None, "pause_sender": None, "actions": []}

    return {
        "should_takeover": True,
        "should_pause": True,
        "pause_chat": chat_who,
        "pause_sender": getattr(message, "sender", None) if _is_group_chat(chat_who, group_chats=group_chats, chat_type=chat_type) else None,
        "actions": list(
            iter_custom_forward_actions(
                matched_rules,
                chat_who,
                message,
                group_chats=group_chats,
                chat_type=chat_type,
                default_target=default_target,
            )
        ),
    }
