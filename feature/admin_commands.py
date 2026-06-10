"""Admin command dispatch helpers."""

from feature import admin_control
from feature.admin_status import (
    build_auto_reply_status_message,
    build_listener_list_message,
    build_status_message,
)


HELP_TEXT = """可用指令

查看状态：
/状态
/监听列表
/自动回复状态
/人设列表
/当前会话

监听管理：
/添加监听 XXX
/取消监听 XXX
/屏蔽监听 XXX
/取消屏蔽 XXX
/添加群监听 XXX
/取消群监听 XXX

临时接管：
/暂停私聊
/恢复私聊
/暂停群聊
/恢复群聊
/接管 XXX
/恢复 XXX
/切到 XXX
/暂停

人设切换：
/切换人设 XXX

发朋友圈：
/发圈
/重新生成
/取消发圈

素材转发：
/转发
/取消转发

"""


def dispatch_admin_command(bot, chat, message):
    content = str(getattr(message, "content", "") or "").strip()
    if not content:
        return None
    if content == "/帮助":
        return chat.SendMsg(HELP_TEXT)
    if content == "/状态":
        return chat.SendMsg(build_status_message(bot))
    if content == "/监听列表":
        return chat.SendMsg(build_listener_list_message(bot))
    if content == "/自动回复状态":
        return chat.SendMsg(build_auto_reply_status_message(bot))
    if content == "/当前会话":
        return admin_control.handle_current_session(bot, chat)
    if content == "/人设列表":
        return admin_control.handle_list_personas(bot, chat)
    if content == "/发圈":
        return admin_control.handle_start_moments_draft(bot, chat)
    if content == "/转发":
        return admin_control.handle_start_forward_draft(bot, chat)
    if content == "/取消转发":
        return admin_control.handle_cancel_forward_draft(bot, chat)
    if content == "/重新生成":
        return admin_control.handle_regenerate_moments_draft(bot, chat)
    if content == "/取消发圈":
        return admin_control.handle_cancel_moments_draft(bot, chat)
    if content.startswith("/切换人设"):
        return admin_control.handle_switch_persona(bot, chat, message)
    if content.startswith("/添加监听"):
        return admin_control.handle_add_listener(bot, chat, message)
    if content.startswith("/取消监听"):
        return admin_control.handle_remove_listener(bot, chat, message)
    if content.startswith("/屏蔽监听"):
        return admin_control.handle_block_listener(bot, chat, message)
    if content.startswith("/取消屏蔽"):
        return admin_control.handle_unblock_listener(bot, chat, message)
    if content.startswith("/添加群监听"):
        return admin_control.handle_add_group_listener(bot, chat, message)
    if content.startswith("/取消群监听"):
        return admin_control.handle_remove_group_listener(bot, chat, message)
    if content == "/暂停私聊":
        return admin_control.handle_pause_private_reply(bot, chat)
    if content == "/恢复私聊":
        return admin_control.handle_resume_private_reply(bot, chat)
    if content == "/暂停群聊":
        return admin_control.handle_pause_group_reply(bot, chat)
    if content == "/恢复群聊":
        return admin_control.handle_resume_group_reply(bot, chat)
    if content.startswith("/接管"):
        return admin_control.handle_take_over_friend(bot, chat, message)
    if content.startswith("/恢复"):
        return admin_control.handle_restore_friend(bot, chat, message)
    if content.startswith("/切到"):
        return admin_control.handle_switch_takeover_friend(bot, chat, message)
    if content == "/暂停":
        return admin_control.handle_pause_current_session(bot, chat)
    if content.startswith("/"):
        return chat.SendMsg(f"未识别的管理员指令：{content}\n发送 /帮助 查看可用指令")
    return None
