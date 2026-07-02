"""Admin runtime control helpers."""

import os
import re

from core import runtime_chat_state
from feature import takeover_runtime
from feature.listening import add_listen_chat_once, remove_listen_chat_verified


def set_runtime_config_value(bot, key, value):
    if hasattr(bot.config, "set_config"):
        bot.config.set_config(key, value)
        return
    setattr(bot.config, key, value)
    if hasattr(bot.config, "config") and isinstance(bot.config.config, dict):
        bot.config.config[key] = value


def set_runtime_config_list(bot, key, values):
    cleaned = []
    seen = set()
    for item in values or []:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    set_runtime_config_value(bot, key, cleaned)
    return cleaned


def handle_list_personas(bot, chat):
    """处理 /人设列表 指令：列出所有可用人设名称。"""
    try:
        files = sorted([f[:-3] for f in os.listdir(bot.config.prompt_dir) if f.endswith(".md")])
    except Exception:
        files = []
    visible = [name for name in files if not name.endswith("-人设近况")]
    if not visible:
        return chat.SendMsg("当前没有可用的人设")
    current = bot.config.default_prompt
    lines = ["人设列表（* 为当前人设）："]
    for name in visible:
        mark = "* " if name == current else "  "
        lines.append(f"{mark}{name}")
    return chat.SendMsg("\n".join(lines))


def handle_switch_persona(bot, chat, message):
    """处理 /切换人设 xxx 指令：切换默认人设。"""
    name = re.sub("/切换人设", "", message.content).strip()
    if not name:
        return chat.SendMsg("请提供人设名称，如：/切换人设 小东-知己版")
    path = os.path.join(bot.config.prompt_dir, f"{name}.md")
    if not os.path.exists(path):
        return chat.SendMsg(f"人设「{name}」不存在")
    set_runtime_config_value(bot, "default_prompt", name)
    return chat.SendMsg(f"当前人设已切换为：{name}")


def handle_add_listener(bot, chat, message):
    """处理 /添加监听 xxx 指令：将好友加入监听名单，并从屏蔽名单中移除。"""
    name = re.sub("/添加监听", "", message.content).strip()
    if not name:
        return chat.SendMsg("请提供好友昵称，如：/添加监听 张三")
    listen_list = list(getattr(bot.config, "listen_list", []) or [])
    blocked_list = list(getattr(bot.config, "global_blacklist", []) or [])
    original_listen_list = list(listen_list)
    original_blocked_list = list(blocked_list)
    if getattr(bot.config, "AllListen_switch", False):
        if name in blocked_list:
            blocked_list.remove(name)
            set_runtime_config_list(bot, "global_blacklist", blocked_list)
            return chat.SendMsg(f"已恢复监听：{name}")
        return chat.SendMsg(f"当前为全部监听模式，无需添加监听：{name}")
    if name in blocked_list:
        blocked_list.remove(name)
        set_runtime_config_list(bot, "global_blacklist", blocked_list)
    if name not in listen_list:
        listen_list.append(name)
        set_runtime_config_list(bot, "listen_list", listen_list)
    if getattr(bot, "wx", None):
        result = add_listen_chat_once(bot, name, "监听")
        if result:
            runtime_chat_state.remember_listen_chat(bot, name, result)
        else:
            set_runtime_config_list(bot, "listen_list", original_listen_list)
            set_runtime_config_list(bot, "global_blacklist", original_blocked_list)
            error_message = result.get("message", "未知错误") if isinstance(result, dict) else "未知错误"
            return chat.SendMsg(f"添加监听失败\n{error_message}")
    return chat.SendMsg(f"已加入监听：{name}")


def handle_remove_listener(bot, chat, message):
    """处理 /取消监听 xxx 指令：将好友移出监听名单。"""
    name = re.sub("/取消监听", "", message.content).strip()
    if not name:
        return chat.SendMsg("请提供好友昵称，如：/取消监听 张三")
    listen_list = list(getattr(bot.config, "listen_list", []) or [])
    if name in listen_list:
        listen_list.remove(name)
        set_runtime_config_list(bot, "listen_list", listen_list)
    if getattr(bot, "wx", None):
        remove_listen_chat_verified(bot, name)
    runtime_chat_state.remove_listen_chat(bot, name)
    return chat.SendMsg(f"已取消监听：{name}")


def handle_block_listener(bot, chat, message):
    """处理 /屏蔽监听 xxx 指令：从监听名单移除，并加入屏蔽名单。"""
    name = re.sub("/屏蔽监听", "", message.content).strip()
    if not name:
        return chat.SendMsg("请提供好友昵称，如：/屏蔽监听 张三")
    listen_list = list(getattr(bot.config, "listen_list", []) or [])
    blocked_list = list(getattr(bot.config, "global_blacklist", []) or [])
    if name in listen_list:
        listen_list.remove(name)
        set_runtime_config_list(bot, "listen_list", listen_list)
    if name not in blocked_list:
        blocked_list.append(name)
        set_runtime_config_list(bot, "global_blacklist", blocked_list)
    if getattr(bot, "wx", None):
        remove_listen_chat_verified(bot, name)
    runtime_chat_state.remove_listen_chat(bot, name)
    return chat.SendMsg(f"已屏蔽监听：{name}")


def handle_unblock_listener(bot, chat, message):
    """处理 /取消屏蔽 xxx 指令：从屏蔽名单移除。"""
    name = re.sub("/取消屏蔽", "", message.content).strip()
    if not name:
        return chat.SendMsg("请提供好友昵称，如：/取消屏蔽 张三")
    blocked_list = list(getattr(bot.config, "global_blacklist", []) or [])
    if name in blocked_list:
        blocked_list.remove(name)
        set_runtime_config_list(bot, "global_blacklist", blocked_list)
    return chat.SendMsg(f"已取消屏蔽：{name}")


def handle_add_group_listener(bot, chat, message):
    """处理 /添加群监听 xxx 指令。"""
    name = re.sub("/添加群监听", "", message.content).strip()
    if not name:
        return chat.SendMsg("请提供群名称，如：/添加群监听 产品群")
    groups = list(getattr(bot.config, "group", []) or [])
    original_groups = list(groups)
    if name not in groups:
        groups.append(name)
        set_runtime_config_list(bot, "group", groups)
    if getattr(bot.config, "group_switch", False) and getattr(bot, "wx", None):
        result = add_listen_chat_once(bot, name, "群监听")
        if result:
            runtime_chat_state.remember_listen_chat(bot, name, result)
        else:
            set_runtime_config_list(bot, "group", original_groups)
            error_message = result.get("message", "未知错误") if isinstance(result, dict) else "未知错误"
            return chat.SendMsg(f"添加群监听失败\n{error_message}")
    return chat.SendMsg(f"已加入群监听：{name}")


def handle_remove_group_listener(bot, chat, message):
    """处理 /取消群监听 xxx 指令。"""
    name = re.sub("/取消群监听", "", message.content).strip()
    if not name:
        return chat.SendMsg("请提供群名称，如：/取消群监听 产品群")
    groups = list(getattr(bot.config, "group", []) or [])
    if name in groups:
        groups.remove(name)
        set_runtime_config_list(bot, "group", groups)
    if getattr(bot, "wx", None):
        remove_listen_chat_verified(bot, name)
    runtime_chat_state.remove_listen_chat(bot, name)
    return chat.SendMsg(f"已取消群监听：{name}")


def handle_pause_private_reply(bot, chat):
    set_runtime_config_value(bot, "chat_listen_only", True)
    return chat.SendMsg("私聊已开启只监听不 AI 回复；监听、记忆、关键词回复和自定义转发保持运行")


def handle_resume_private_reply(bot, chat):
    set_runtime_config_value(bot, "chat_listen_only", False)
    bot._pause_chat_reply = False
    return chat.SendMsg("私聊只监听不 AI 回复已关闭，AI 自动回复已恢复")


def handle_pause_group_reply(bot, chat):
    set_runtime_config_value(bot, "group_listen_only", True)
    return chat.SendMsg("群聊已开启只监听不 AI 回复；监听、记忆、关键词回复和自定义转发保持运行")


def handle_resume_group_reply(bot, chat):
    set_runtime_config_value(bot, "group_listen_only", False)
    bot._pause_group_reply = False
    return chat.SendMsg("群聊只监听不 AI 回复已关闭，AI 自动回复已恢复")


def _is_known_takeover_target(bot, target):
    target = str(target or "").strip()
    if not target:
        return False
    evidence_seen = False

    listen_list = getattr(getattr(bot, "config", None), "listen_list", None)
    if listen_list is not None:
        evidence_seen = True
        if target in list(listen_list or []):
            return True

    blocked_list = getattr(getattr(bot, "config", None), "global_blacklist", None)
    if blocked_list is not None:
        evidence_seen = True
        if target in list(blocked_list or []):
            return True

    listen_cache = getattr(bot, "_listen_chats", None)
    if isinstance(listen_cache, dict):
        evidence_seen = True
        if target in listen_cache:
            return True

    paused = getattr(bot, "_pause_chat_reply_users", None)
    if paused:
        evidence_seen = True
        if target in set(paused):
            return True

    runtime_list = getattr(bot, "all_Mode_listen_list", None)
    if isinstance(runtime_list, list):
        evidence_seen = True
        for item in runtime_list:
            if isinstance(item, (list, tuple)) and item and str(item[0]).strip() == target:
                return True

    return not evidence_seen


def handle_take_over_friend(bot, chat, message):
    target = re.sub("/接管", "", message.content).strip()
    if not target:
        return chat.SendMsg("请提供好友昵称，如：/接管 张三")
    mode, _ = takeover_runtime.get_workspace_mode(bot)
    if mode == takeover_runtime.MOMENTS_MODE:
        return chat.SendMsg("当前正在发圈，请先完成或取消当前发圈任务后再接管")
    if not _is_known_takeover_target(bot, target):
        return chat.SendMsg(f"未找到好友：{target}，请确认昵称后再接管")
    if runtime_chat_state.pause_single_chat_reply(bot, target):
        takeover_runtime.enter_takeover(bot, target, source="manual")
        takeover_runtime.replay_pending_takeover_messages_to_admin(bot, target)
        return chat.SendMsg(takeover_runtime.takeover_enter_reply())
    return chat.SendMsg("请提供有效的好友昵称")


def handle_restore_friend(bot, chat, message):
    target = re.sub("/恢复", "", message.content).strip()
    if not target:
        return chat.SendMsg("请提供好友昵称，如：/恢复 张三")
    if runtime_chat_state.resume_single_chat_reply(bot, target):
        marker = getattr(bot, "_mark_context_repair_needed_after_restore", None)
        if callable(marker):
            marker(target)
        takeover_runtime.clear_pending_takeover_messages(bot, target)
        takeover_runtime.clear_takeover(bot, target)
        return chat.SendMsg(f"{target} 的自动回复已恢复")
    return chat.SendMsg(f"{target} 当前不在接管列表中")


def handle_current_session(bot, chat):
    return chat.SendMsg(takeover_runtime.build_current_session_message(bot))


def handle_pause_current_session(bot, chat):
    return chat.SendMsg(takeover_runtime.end_current_session(bot))


def handle_switch_takeover_friend(bot, chat, message):
    target = re.sub("/切到", "", message.content).strip()
    if not target:
        return chat.SendMsg("请提供好友昵称，如：/切到 张三")
    mode, _ = takeover_runtime.get_workspace_mode(bot)
    if mode == takeover_runtime.MOMENTS_MODE:
        return chat.SendMsg("当前正在发圈，请先完成或取消当前发圈任务后再切换接管好友")
    if not takeover_runtime.switch_takeover(bot, target, source="manual"):
        return chat.SendMsg(f"{target} 当前不在接管列表中，请先使用 /接管 {target}")
    takeover_runtime.replay_pending_takeover_messages_to_admin(bot, target)
    return chat.SendMsg(takeover_runtime.takeover_enter_reply())


def handle_toggle_simple_switch(bot, chat, key, enabled, success_text):
    set_runtime_config_value(bot, key, bool(enabled))
    return chat.SendMsg(success_text)


def handle_start_moments_draft(bot, chat):
    mode, target = takeover_runtime.get_workspace_mode(bot)
    if mode == takeover_runtime.TAKEOVER_MODE and target:
        return chat.SendMsg(f"当前正在接管：{target}，请先 /恢复 {target} 后再发圈")
    return bot.start_admin_moments_draft(chat)


def handle_regenerate_moments_draft(bot, chat):
    return bot.regenerate_admin_moments_draft(chat)


def handle_publish_moments_draft(bot, chat, message):
    content = str(getattr(message, "content", "") or "").strip()
    if content == "/发布":
        candidate_index = 1
    else:
        match = re.fullmatch(r"/([1-3])", content)
        if not match:
            return chat.SendMsg("发布命令格式为 /发布 或 /1 /2 /3")
        candidate_index = int(match.group(1))
    return bot.publish_admin_moments_draft(chat, candidate_index=candidate_index)


def handle_cancel_moments_draft(bot, chat):
    return bot.cancel_admin_moments_draft(chat)


def handle_start_forward_draft(bot, chat):
    mode, target = takeover_runtime.get_workspace_mode(bot)
    if mode == takeover_runtime.TAKEOVER_MODE and target:
        return chat.SendMsg(f"当前正在接管：{target}，请先 /恢复 {target} 后再转发")
    if mode == takeover_runtime.MOMENTS_MODE:
        return chat.SendMsg("当前正在发圈，请先 /取消发圈 后再转发")
    return bot.start_admin_forward_draft(chat)


def handle_cancel_forward_draft(bot, chat):
    return bot.cancel_admin_forward_draft(chat)
