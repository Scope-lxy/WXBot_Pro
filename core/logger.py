from datetime import datetime
import os
import re
import sys
import threading
import traceback

def _base_dir():
    """运行时基础目录：打包后为 exe 所在目录，开发时为当前目录"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.dirname(sys.executable)
    return os.path.abspath(".")

DATA_DIR = os.path.join(_base_dir(), "data")
LOG_PATH = os.path.join(_base_dir(), "panel_logs")
os.makedirs(LOG_PATH, exist_ok=True)
# 日志颜色映射
LOG_COLORS = {
    'INFO': 'text-primary',
    'WARNING': 'text-warning',
    'WARN': 'text-warning',
    'ERROR': 'text-danger',
    'DEBUG': 'text-secondary',
    'SUCCESS': 'text-success'
}

log_messages = []
_log_lock = threading.Lock()
_next_log_id = 0
_thread_exception_logger_installed = False

MESSAGE_TYPE_LABELS = {
    "text": "文本",
    "voice": "语音",
    "image": "图片",
    "video": "视频",
    "file": "文件",
    "quote": "引用",
}

MESSAGE_ATTR_SCENE_LABELS = {
    "friend": "私聊",
    "self": "自己",
    "system": "系统",
}

SPLIT_SOURCE_LABELS = {
    "换行": "换行",
    "句末标点": "句末标点",
    "中文空格停顿": "中文空格停顿",
}

def _copy_logs(items):
    return [dict(item) for item in items]

def _normalize_level(level):
    level_text = str(level or "INFO").upper()
    if level_text == "WARN":
        return "WARNING"
    return level_text

def _strip_duplicate_timestamp(message):
    text = str(message or "")
    return re.sub(
        r"^\s*(?:\[\s*)?\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}:\d{2}(?:\s*\])?\s*[:：-]?\s*",
        "",
        text,
        count=1,
    )

def _message_type_label(value):
    key = str(value or "").strip().lower()
    return MESSAGE_TYPE_LABELS.get(key, key or "未知")

def _message_scene_label(attr, window):
    attr_key = str(attr or "").strip().lower()
    if attr_key == "group":
        return "群聊"
    if attr_key == "friend":
        return "私聊"
    if attr_key in MESSAGE_ATTR_SCENE_LABELS:
        return MESSAGE_ATTR_SCENE_LABELS[attr_key]
    window_text = str(window or "").strip()
    if window_text:
        return "消息"
    return "日志"

def _format_message_event(match):
    msg_type = _message_type_label(match.group("type"))
    attr = match.group("attr")
    window = match.group("window").strip()
    sender = match.group("sender").strip()
    content = match.group("content").strip()
    scene = _message_scene_label(attr, window)
    parts = [f"{scene} {window}：收到{msg_type}消息"]
    if sender:
        parts.append(f"发送人：{sender}")
    if content:
        parts.append(f"内容：{content}")
    return "，".join(parts)

def _format_api_success(match):
    api_name = match.group("api").strip()
    stream = match.groupdict().get("stream") or ""
    chunks = match.groupdict().get("chunks") or ""
    content = match.group("content").strip()
    detail = api_name
    if stream:
        detail = f"{detail}，{stream}"
    parts = [f"API返回成功（{detail}）"]
    if chunks:
        parts.append(f"块数：{chunks}")
    if content:
        parts.append(f"内容：{content}")
    return "，".join(parts)

def _format_api_failure(match):
    api_name = match.group("api").strip()
    error_type = match.groupdict().get("error_type") or ""
    detail = match.group("detail").strip()
    parts = [f"API调用失败（{api_name}）"]
    if error_type:
        parts.append(f"错误类型：{error_type}")
    if detail:
        parts.append(f"详情：{detail}")
    return "，".join(parts)

def _format_retry_failure(match):
    api_name = match.group("api").strip()
    attempt = match.group("attempt").strip()
    error = match.group("error").strip()
    delay = match.group("delay").strip()
    return f"API调用失败（{api_name}），重试：第 {attempt} 次失败，{delay}s 后重试，详情：{error}"

def _format_listener_delete_result(match):
    target = match.group("target").strip()
    detail = match.group("detail").strip()
    if detail.lower() in {"ok", "success", "true"} or detail in {"成功", "已成功"}:
        return f"监听管理 {target}：删除监听完成"
    if detail:
        return f"监听管理 {target}：删除监听结果：{detail}"
    return f"监听管理 {target}：删除监听完成"

def format_log_message(message):
    """Normalize legacy log text into a user-scannable order."""
    text = _strip_duplicate_timestamp(message).strip()
    if not text:
        return ""

    replacements = [
        (
            re.compile(r"^类型：(?P<type>\S+)\s+属性：(?P<attr>\S+)\s+窗口：(?P<window>.+?)\s+发送人：(?P<sender>.*?)\s*-\s*消息：(?P<content>.*)$", re.S),
            _format_message_event,
        ),
        (
            re.compile(r"^(?P<api>Chat Completions)\s+(?P<stream>流式)返回成功（共\s*(?P<chunks>\d+)\s*个块）：(?P<content>.*)$", re.S),
            _format_api_success,
        ),
        (
            re.compile(r"^(?P<api>Chat Completions)\s+(?P<stream>非流式)返回成功：(?P<content>.*)$", re.S),
            _format_api_success,
        ),
        (
            re.compile(r"^(?P<api>Responses API)\s+返回成功：(?P<content>.*)$", re.S),
            _format_api_success,
        ),
        (
            re.compile(r"^(?P<api>DusAPI (?:Claude|GPT))\s+(?P<stream>流式)返回成功：(?P<content>.*)$", re.S),
            _format_api_success,
        ),
        (
            re.compile(r"^(?P<api>DusAPI (?:Claude|GPT))\s+返回成功：(?P<content>.*)$", re.S),
            _format_api_success,
        ),
        (
            re.compile(r"^(?P<api>DusAPI (?:Claude|GPT))\s+第\s*(?P<attempt>\d+)\s*次重试成功：(?P<content>.*)$", re.S),
            lambda match: f"API返回成功（{match.group('api').strip()}），重试：第 {match.group('attempt').strip()} 次，内容：{match.group('content').strip()}",
        ),
        (
            re.compile(r"^(?P<api>Chat Completions|Responses API)\s+(?:API\s+)?调用失败\s+\[(?P<error_type>.*?)\]\s*[:：]?\s*(?P<detail>.*)$", re.S),
            _format_api_failure,
        ),
        (
            re.compile(r"^(?P<api>DusAPI (?:Claude|GPT))\s+第\s*(?P<attempt>\d+)\s*次失败（(?P<error>.*?)），(?P<delay>\d+)s 后重试\.*$", re.S),
            _format_retry_failure,
        ),
        (
            re.compile(r"^(?P<scene>私聊|群聊)\s+(?P<chat>.+?)\s+命中(?P<source>换行|句末标点|中文空格停顿)自动拆分，共\s*(?P<count>\d+)\s*条$", re.S),
            lambda match: f"{match.group('scene')} {match.group('chat').strip()}：命中{SPLIT_SOURCE_LABELS.get(match.group('source'), match.group('source'))}，已自动拆分成 {match.group('count')} 条",
        ),
        (
            re.compile(r"^(?P<scene>私聊|群组|群聊)\s+(?P<chat>.+?)\s+已启用只监听不AI回复，跳过 AI 调用$", re.S),
            lambda match: f"{match.group('scene')} {match.group('chat').strip()}：只监听不 AI 回复，已跳过 AI 调用",
        ),
        (
            re.compile(r"^(?P<scene>私聊|群组|群聊)\s+(?P<chat>.+?)\s+关键字消息：(?P<content>.*)$", re.S),
            lambda match: f"{match.group('scene')} {match.group('chat').strip()}：命中关键词回复，内容：{match.group('content').strip()}",
        ),
        (
            re.compile(r"^处理\s+(?P<chat>.+?)\s+窗口\s+(?P<sender>.*?)\s+消息：(?P<content>.*)$", re.S),
            lambda match: f"消息处理 {match.group('chat').strip()}：开始处理，发送人：{match.group('sender').strip()}，内容：{match.group('content').strip()}",
        ),
        (
            re.compile(r"^会话记忆已增量更新：(?P<chat>.+)$", re.S),
            lambda match: f"会话记忆 {match.group('chat').strip()}：已增量更新",
        ),
        (
            re.compile(r"^会话记忆自动维护失败：(?P<detail>.*)$", re.S),
            lambda match: f"会话记忆：自动维护失败，详情：{match.group('detail').strip()}",
        ),
        (
            re.compile(r"^添加(?P<label>好友|群组|监听)?\s*(?P<target>.+?)\s+监听完成$", re.S),
            lambda match: f"监听管理 {match.group('target').strip()}：添加监听完成",
        ),
        (
            re.compile(r"^添加(?P<label>好友|群组|监听)?\s*(?P<target>.+?)\s+监听失败[,:：]?\s*(?P<detail>.*)$", re.S),
            lambda match: f"监听管理 {match.group('target').strip()}：添加监听失败，详情：{match.group('detail').strip()}",
        ),
        (
            re.compile(r"^(?:获取|关闭)(?P<kind>全部监听子窗口|监听子窗口|残留监听子窗口)\s*(?P<target>.*?)\s*失败[,:：]\s*(?P<detail>.*)$", re.S),
            lambda match: f"监听管理 {match.group('target').strip() or '全部窗口'}：{match.group('kind').strip()}失败，详情：{match.group('detail').strip()}",
        ),
        (
            re.compile(r"^(?P<target>.+?)\s+删除监听返回[：:]\s*(?P<detail>.*)$", re.S),
            _format_listener_delete_result,
        ),
        (
            re.compile(r"^(?P<target>.+?)\s+删除监听校验通过$", re.S),
            lambda match: f"监听管理 {match.group('target').strip()}：删除监听校验通过",
        ),
        (
            re.compile(r"^(?P<target>.+?)\s+删除监听后仍有残留子窗口，尝试直接关闭$", re.S),
            lambda match: f"监听管理 {match.group('target').strip()}：删除监听后仍有残留子窗口，正在尝试直接关闭",
        ),
        (
            re.compile(r"^(?P<target>.+?)\s+残留监听子窗口(?P<action>直接关闭已执行，正在复查|已关闭|关闭失败.*|直接关闭未成功.*)$", re.S),
            lambda match: f"监听管理 {match.group('target').strip()}：残留监听子窗口{match.group('action').strip()}",
        ),
        (
            re.compile(r"^\[(?P<module>[^\]\n]{2,30})\]\s*(?P<body>.*)$", re.S),
            lambda match: f"{match.group('module').strip()}：{match.group('body').strip()}",
        ),
        (
            re.compile(r"^【(?P<module>[^】\n]{2,30})】\s*(?P<body>.*)$", re.S),
            lambda match: f"{match.group('module').strip()}：{match.group('body').strip()}",
        ),
    ]

    for pattern, formatter in replacements:
        match = pattern.match(text)
        if match:
            return formatter(match).strip()

    return text

def log_server(level, msg):
    """
    记录日志到内存和文件
    :param level: 日志级别 (INFO, WARNING, ERROR, DEBUG, SUCCESS)
    :param msg: 日志消息
    """
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    level = _normalize_level(level)
    msg = format_log_message(msg)
    global _next_log_id
    with _log_lock:
        _next_log_id += 1
        log_entry = {
            'id': _next_log_id,
            'time': timestamp,
            'level': level,
            'message': msg,
            'color': LOG_COLORS.get(level.upper(), 'text-dark')
        }
        log_messages.append(log_entry)
        
        # 限制日志数量，避免内存占用过大
        if len(log_messages) > 1000:
            log_messages.pop(0)
    
    # 同时输出到控制台
    print(f"[{timestamp}] [{level}] {msg}")

def get_recent_logs(limit=50):
    with _log_lock:
        return _copy_logs(log_messages[-limit:])

def get_logs_after(after_id, limit=50):
    with _log_lock:
        if not log_messages:
            return {
                'logs': [],
                'reset': False,
            }

        if after_id is None or after_id <= 0:
            return {
                'logs': _copy_logs(log_messages[-limit:]),
                'reset': True,
            }

        earliest_id = log_messages[0]['id']
        if after_id < earliest_id:
            return {
                'logs': _copy_logs(log_messages[-limit:]),
                'reset': True,
            }

        return {
            'logs': _copy_logs([item for item in log_messages if item['id'] > after_id]),
            'reset': False,
        }
def log(level="INFO", message=''):
    """日志输出"""
    timestamp = datetime.now().strftime("%m-%d %H:%M:%S")
    level = _normalize_level(level)
    message = format_log_message(message)
    colors = {
        "INFO": "#691bfd",
        "WARNING": "#FFA500",
        "ERROR": "#FF0000",
        "DEBUG": "#00CC33"
    }
    # qt6日志输出
    """color = colors.get(level, "#00CC33")
    formatted_message = f'<span style="color:{color}">[{timestamp}]: {message}</span>'
    main_window.textEdit_log.append(formatted_message)
    scrollbar = main_window.textEdit_log.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())"""
    # server端日志输出
    log_server(level, message)
    # 终端日志输出
    # print(f'[{timestamp}]: {message}')
    # 写入log到本地
    now_day = datetime.now().strftime("%y%m%d")
    try:
        with open(os.path.join(LOG_PATH, f'log_{now_day}.txt'), 'a', encoding='utf-8-sig') as f:
            f.write(f'[{timestamp}]: {message}' + '\n')
    except Exception as e:
        print(f"写入日志文件失败: {e}")


def install_thread_exception_logger():
    """Route uncaught background thread exceptions into the normal panel log."""
    global _thread_exception_logger_installed
    if _thread_exception_logger_installed:
        return False
    previous_hook = getattr(threading, "excepthook", None)

    def _log_thread_exception(args):
        if args.exc_type is SystemExit:
            if callable(previous_hook):
                previous_hook(args)
            return
        thread_name = getattr(getattr(args, "thread", None), "name", "") or "unknown"
        exc_type = getattr(args, "exc_type", None)
        exc_value = getattr(args, "exc_value", None)
        exc_name = getattr(exc_type, "__name__", str(exc_type or "Exception"))
        try:
            tb_text = "".join(traceback.format_exception(exc_type, exc_value, getattr(args, "exc_traceback", None))).strip()
        except Exception:
            tb_text = str(exc_value or "")
        if len(tb_text) > 4000:
            tb_text = tb_text[:4000] + "\n... traceback truncated ..."
        log(
            level="ERROR",
            message=f"[后台线程异常] {thread_name}: {exc_name}: {exc_value}\n{tb_text}",
        )
        if callable(previous_hook) and previous_hook is not _log_thread_exception:
            try:
                previous_hook(args)
            except Exception:
                pass

    threading.excepthook = _log_thread_exception
    _thread_exception_logger_installed = True
    return True
