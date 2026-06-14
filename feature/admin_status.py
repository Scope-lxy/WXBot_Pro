"""Admin runtime status rendering helpers."""

from core.api import format_api_display_name
from core import runtime_chat_state
from feature.ai_material_outreach import AI_AUTO_OUTREACH_TASK_ID
from feature.material_outreach import build_ai_candidate_material_cards
from feature import takeover_runtime


def format_name_list(items):
    cleaned = []
    seen = set()
    for item in items or []:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return "、".join(cleaned) if cleaned else "（无）"


def summarize_dual_switch(chat_enabled, group_enabled):
    if chat_enabled and group_enabled:
        return "开启"
    if (not chat_enabled) and (not group_enabled):
        return "关闭"
    return "部分开启"


def summarize_reply_limit(config):
    if not bool(getattr(config, "text_reply_limit_switch", False)):
        return "关闭"
    limit = int(getattr(config, "text_reply_limit_count", 99) or 0)
    limit_hours = int(getattr(config, "text_reply_limit_hours", 24) or 0)
    if limit <= 0 or limit_hours <= 0:
        return "不限制"
    return f"{limit}条/{limit_hours}小时"


def _count_enabled_tasks(items):
    count = 0
    for item in items or []:
        if isinstance(item, dict) and item.get("enabled", True):
            count += 1
    return count


def _material_outreach_counts(bot):
    load_send_records = getattr(bot, "_load_material_send_records", None)
    if callable(load_send_records):
        send_records = load_send_records()
        material_count = 0
        ai_count = 0
        for record in send_records:
            if not isinstance(record, dict) or not record.get("success"):
                continue
            if record.get("task_id") == AI_AUTO_OUTREACH_TASK_ID:
                ai_count += 1
            else:
                material_count += 1
        return material_count, ai_count
    return (
        int(getattr(bot, "_material_outreach_count", 0) or 0),
        int(getattr(bot, "_ai_material_outreach_count", 0) or 0),
    )


def _runtime_daily_stats(bot):
    getter = getattr(bot, "get_daily_runtime_stats", None)
    stats = getter() if callable(getter) else {}
    material_count, ai_count = _material_outreach_counts(bot)
    return {
        "received_messages": int((stats or {}).get("received_messages", getattr(bot, "msg_received_count", 0)) or 0),
        "replied_messages": int((stats or {}).get("replied_messages", getattr(bot, "msg_replied_count", 0)) or 0),
        "scheduled_messages_sent": int((stats or {}).get("scheduled_messages_sent", 0) or 0),
        "material_forwards_sent": int((stats or {}).get("material_forwards_sent", material_count) or 0),
        "ai_material_forwards_sent": int((stats or {}).get("ai_material_forwards_sent", ai_count) or 0),
        "moments_published": int((stats or {}).get("moments_published", 0) or 0),
        "chat_api_requests": int((stats or {}).get("chat_api_requests", 0) or 0),
        "other_api_requests": int((stats or {}).get("other_api_requests", 0) or 0),
    }


def _ai_available_material_count(bot):
    load_materials = getattr(bot, "_load_material_outreach_materials", None)
    if callable(load_materials):
        return len(build_ai_candidate_material_cards(load_materials()))
    return int(getattr(bot, "_ai_outreach_available_material_count", 0) or 0)


def _current_interface_name(bot):
    get_runtime_name = getattr(bot, "_get_current_chat_api_display_name", None)
    if callable(get_runtime_name):
        try:
            return str(get_runtime_name() or "").strip() or "未连接"
        except Exception:
            pass
    api_configs = getattr(getattr(bot, "config", None), "api_configs", []) or []
    try:
        index = int(getattr(bot, "active_chat_api_index", getattr(bot.config, "api_index", 0)) or 0)
    except (AttributeError, TypeError, ValueError):
        index = 0
    return format_api_display_name(api_configs, index, fallback="未连接")


def build_listener_list_message(bot):
    config = bot.config
    if bool(getattr(config, "AllListen_switch", False)):
        friend_text = "全部私聊好友（当前为全部监听模式）"
    else:
        friend_text = format_name_list(getattr(config, "listen_list", []))
    return "\n".join([
        "监听的群：",
        format_name_list(getattr(config, "group", [])),
        "",
        "监听的好友：",
        friend_text,
        "",
        "已屏蔽的好友：",
        format_name_list(getattr(config, "global_blacklist", [])),
    ])


def build_auto_reply_status_message(bot):
    config = bot.config
    paused = sorted(runtime_chat_state.ensure_pause_chat_reply_users(bot))
    paused_text = "、".join(paused) if paused else "无"
    return "\n".join([
        "自动回复状态",
        "",
        f"私聊自动回复：{'只监听不 AI 回复' if getattr(config, 'chat_listen_only', False) or getattr(bot, '_pause_chat_reply', False) else '开启'}",
        f"群聊自动回复：{'只监听不 AI 回复' if getattr(config, 'group_listen_only', False) or getattr(bot, '_pause_group_reply', False) else '开启'}",
        f"人工接管好友：{len(paused)} 个（{paused_text}）",
    ])


def build_status_message(bot):
    config = bot.config
    daily_stats = _runtime_daily_stats(bot)
    scheduled_task_count = _count_enabled_tasks(getattr(config, "scheduled_message_task_list", []))
    material_task_count = _count_enabled_tasks(getattr(config, "material_outreach_list", []))
    ai_available_material_count = _ai_available_material_count(bot)
    paused = sorted(runtime_chat_state.ensure_pause_chat_reply_users(bot))
    paused_text = "、".join(paused) if paused else "无"
    private_mode = "全部监听" if bool(getattr(config, "AllListen_switch", False)) else "名单监听"
    group_mode = "开启" if bool(getattr(config, "group_switch", False)) else "关闭"
    lines = [
        "机器人状态",
        "",
        f"运行时间：{config.get_run_time(bot.start_time)}",
        f"工作模式：{takeover_runtime.describe_workspace(bot)}",
        f"当前接口：{_current_interface_name(bot)}",
        f"当前人设：{getattr(config, 'default_prompt', '默认')}",
        f"私聊监听模式：{private_mode}",
        f"群聊监听模式：{group_mode}",
        "",
        "数据统计：",
        f"已收消息：{daily_stats['received_messages']} 条",
        f"已回复消息：{daily_stats['replied_messages']} 次",
        f"API请求数：{daily_stats['chat_api_requests'] + daily_stats['other_api_requests']} 次",
        f"聊天请求：{daily_stats['chat_api_requests']} 次",
        f"其他请求：{daily_stats['other_api_requests']} 次",
        f"定时消息：{daily_stats['scheduled_messages_sent']} 次",
        f"素材转发：{daily_stats['material_forwards_sent']} 次",
        f"AI转发次数：{daily_stats['ai_material_forwards_sent']} 次",
        f"发朋友圈：{daily_stats['moments_published']} 次",
        "",
        f"人工接管好友：{len(paused)} 个（{paused_text}）",
        "",
        "关键功能：",
        f"定时消息：{'开启' if scheduled_task_count else '关闭'}（任务数量：{scheduled_task_count}）",
        f"素材转发：{'开启' if material_task_count else '关闭'}（任务数量：{material_task_count}）",
        f"AI自动转发：{'开启' if bool(getattr(config, 'ai_material_outreach_switch', False)) else '关闭'}（可用素材：{ai_available_material_count}）",
    ]
    return "\n".join(lines)
