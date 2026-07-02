# /mnt/data/web_server.py
# ---------------------------------------------
# 机器人管理网页（含关键词与群欢迎概率扩展）
# ---------------------------------------------
"""
机器人管理网页
使用 Flask 框架开发，提供机器人控制、配置管理等功能
"""
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file
import json
import os
import re
import shutil
import tempfile
import subprocess
import struct
import zlib
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import datetime, timedelta
import logging
from functools import wraps
import threading
from pathlib import Path
from functools import lru_cache
from core.api import (
    APIConfigSnapshot,
    DusAPI,
    OpenAIAPI,
    build_api_config_snapshot,
    format_api_display_name,
    get_tts_sdk_meta,
    list_tts_model_options,
    list_tts_sdk_options,
    normalize_api_protocol,
    normalize_tts_settings,
    normalize_reasoning_effort,
    resolve_tts_preview_payload,
    set_chat_api_app_version,
)
from wxbot_core import WXBot, version as BOT_VERSION
from core.logger import log
import core.logger as logger
from core.prompt_system import ChatMemoryExtractor, ChatMemoryStore, PromptSystem, PERSONA_STATUS_SUFFIX, SystemPromptStore
from core.account_storage import (
    DEFAULT_ACCOUNT_ID,
    account_area_dir,
    account_dir,
    account_module_dir,
    account_module_file,
    discover_account_ids,
    discover_populated_account_ids,
    ensure_default_account,
    is_known_account_id,
    known_account_ids,
    preferred_account_id,
)
from core.config import coerce_float_range, sanitize_api_capability_map, set_api_capability
from core.config import api_supports_capability
from core.runtime_metrics import RuntimeMetricsStore
from core.memory import read_memory_original_name, resolve_memory_storage_name
from core.chat_history_format import format_memory_record_for_display
from core.identity_index import (
    dismiss_pending as dismiss_identity_pending,
    list_chat_memory_names,
    list_memory_chat_names,
    load_index as load_identity_index,
    reconcile_storage_names as reconcile_identity_storage_names,
    save_index as save_identity_index,
)
from core.contact_profiles import (
    default_directory as default_contact_directory,
    directory_path as contact_directory_path,
    load_directory as load_contact_directory,
    mark_send_name_conflicts,
    normalize_tag_list,
    repair_candidates as contact_repair_candidates,
    save_directory as save_contact_directory,
)
from core.sending import clean_ai_reply_text, sanitize_ai_output_text
from core.tts import TTSConfigError, create_tts_client, make_tts_cache_path
from feature.voice_reply import DEFAULT_CHAT_VOICE_REPLY_KEYWORDS, DEFAULT_GROUP_VOICE_REPLY_KEYWORDS
from core.scheduled_tasks import (
    normalize_fixed_task_schedule,
    normalize_random_task_schedule,
)
from feature.material_outreach import (
    build_ai_candidate_material_cards,
    load_json_list,
    load_json_object,
    material_display_label,
    material_outreach_stats,
    material_outreach_timeline,
    normalize_batch_material_strategy,
    normalize_material_record,
    normalize_material_outreach_task,
    normalize_material_ownership,
    normalize_material_outreach_history_payload,
    normalize_material_outreach_preface_config,
    normalize_material_outreach_runtime_payload,
    normalize_manual_target_names,
    normalize_material_types,
    normalize_trigger_strategy,
    save_json_list,
    save_json_object,
)
from feature.ai_material_outreach import (
    AI_AUTO_OUTREACH_TASK_ID,
    filter_ai_outreach_candidate_pool,
    normalize_ai_material_outreach_config,
)
from feature.keyword_reply import (
    MAX_KEYWORD_REPLY_FILES,
    join_keyword_terms,
    normalize_keyword_terms,
    normalize_keyword_reply_rule,
)
from feature.new_friends import (
    new_friend_welcome_message_has_content,
    normalize_new_friend_welcome_messages,
)
from feature.contacts import (
    coerce_auto_maintenance_full_scan_interval_days,
    coerce_auto_maintenance_interval_minutes,
    coerce_auto_maintenance_window_time,
    normalize_auto_maintenance_batch_size,
    stop_maintenance_hint,
)
from feature import friend_request, relationship_scan
from feature.moments_tasks import (
    MOMENTS_TASK_STATUS_VALUES,
    cancel_queued_moments_task,
    clean_moments_string_list,
    deserialize_moments_task_collection,
    default_moments_publish_time,
    latest_moments_random_window,
    moments_task_has_ai_candidates,
    moments_task_counts,
    new_moments_task_id,
    normalize_moments_task,
    parse_moments_candidates,
    queue_moments_task,
    delete_managed_moments_uploads,
    resolve_moments_execute_after,
    serialize_moments_task_collection,
)

from feature.scheduled_message_tasks import (
    STATUS_RUNNING,
    STATUS_PENDING_CONFIRM,
    STATUS_PENDING,
    build_scheduled_message_task_view,
    deserialize_scheduled_message_task_collection,
    ensure_scheduled_message_next_run,
    normalize_scheduled_message_task_payload,
    queue_scheduled_message_task,
    return_scheduled_message_task,
    serialize_scheduled_message_task_collection,
)
from feature.task_workbench_service import (
    TaskWorkbenchServiceError,
    build_runtime_payload as build_task_workbench_runtime_payload,
    build_workbench_payload as build_task_workbench_payload,
    cancel_queue_item as cancel_task_workbench_queue_item,
    clear_executions as clear_task_workbench_executions,
    queue_task as queue_task_in_workbench,
)
from feature.task_display_titles import (
    material_outreach_record_title,
    material_outreach_task_title,
    moments_task_title,
    scheduled_message_task_title,
)

set_chat_api_app_version(BOT_VERSION)
import pythoncom
import webbrowser
import time
import socket
from extension import email as email_send
from extension import webhook as webhook_send
import ctypes
import atexit
import importlib.metadata
import secrets
from collections import defaultdict, deque
from urllib.parse import unquote, urljoin, urlparse

# fix_paths.py
import sys
def resource_path(relative_path):
    """ 获取资源的绝对路径（打包后指向 _MEIPASS，用于只读资源如 templates）"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

def base_dir():
    """获取运行时基础目录（打包后为 exe 所在目录，开发时为脚本所在目录）"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.dirname(sys.executable)
    return os.path.abspath(".")

# 初始化 Flask 应用
app = Flask(__name__, template_folder=resource_path('templates'), static_folder=resource_path('templates/static'))
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_host=1, x_proto=1, x_prefix=1)

# 安全配置
app.config.update(
    SESSION_COOKIE_SECURE=False,
    SESSION_COOKIE_HTTPONLY=False,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(days=1)
)

# 配置参数
PORT = 10001
DATA_DIR = os.path.join(base_dir(), 'data')
CONFIG_DIR = os.path.join(DATA_DIR, 'config')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
CONTACT_PROFILES_DIR = DATA_DIR
ADMIN_FILE  = os.path.join(CONFIG_DIR, 'admin.json')
EMAIL_FILE  = os.path.join(CONFIG_DIR, 'email.json')
WEBHOOK_FILE = os.path.join(CONFIG_DIR, 'webhook.json')
PROMPT_DIR  = os.path.join(DATA_DIR, 'prompt')
MEMORY_BASE = DATA_DIR
CHAT_MEMORY_BASE = DATA_DIR
BACKUP_BASE = os.path.join(base_dir(), 'backups')
PANEL_LOG_DIR = os.path.join(base_dir(), 'wxbot_logs')
APP_SECRET_FILE = os.path.join(CONFIG_DIR, 'panel_secret.key')
LAST_WX_ID_FILE = os.path.join(CONFIG_DIR, 'last_wx_id.txt')
SIVER_PANEL_BASE_URL = 'https://panel.siver.top'
SIVER_PANEL_WS_URL = 'wss://panel.siver.top/relay/ws'
DEFAULT_PROMPT_CONTENT = "你是一个ai回复助手，请根据用户的问题给出回答,回复尽量保持在30字以内"
DEFAULT_PERSONA_STATUS_CONTENT = """# 人设近况

## 使用说明

这部分不是背景记录，而是你当前的近况，你可以把它当成你要做的事，话题的线索和对话切入点。
你可以在合适的时候主动提到、顺着聊、接着延展，但不要生硬堆砌，也不要把它当成必须逐条复述的说明书。

## 近况条目
在这里输入
"""
DEFAULT_PERSONA_STATUS_TEMPLATE_NAME = f"模板{PERSONA_STATUS_SUFFIX}.md"
PERSONA_STATUS_AVAILABLE_SECTION_RE = re.compile(
    r"^##\s*近况条目\s*$([\s\S]*?)(?=^##\s+|\Z)",
    re.MULTILINE,
)
PERSONA_STATUS_PLACEHOLDER_LINES = {
    "在这里输入",
    "请在这里输入",
    "在这里填写",
    "请在这里填写",
}
API_TEXT_TEST_PROMPT = "你是文本识别连通性测试助手。请只回复 OK。"
API_IMAGE_TEST_PROMPT = "You are a vision capability test assistant. Answer only from the image. Do not guess."
API_IMAGE_TEST_MESSAGE = "What is the color of the single large square in the image? Answer with one color word only."
API_IMAGE_TEST_EXPECTED_COLOR = "red"
API_TEST_MESSAGE = "测试"
API_TEST_MAX_OUTPUT_TOKENS = 64
PANEL_GENERATION_MAX_OUTPUT_TOKENS = 25600
MEMORY_EXTRACTION_MAX_OUTPUT_TOKENS = 25600
DEFAULT_EMAIL_CONFIG = {
    "host": "",
    "port": "",
    "user": "",
    "pass": "",
}
DEFAULT_VOICE_TRANSCRIPTION_FALLBACK_TEXT = "刚才那条语音，我有点没听清"


def _clean_sort_text(value):
    return str(value or '').strip()


def _fallback_sort_bytes(text):
    return _clean_sort_text(text).casefold().encode('utf-8', errors='ignore')


@lru_cache(maxsize=4096)
def _windows_zh_sort_key(text):
    text = _clean_sort_text(text)
    if os.name != 'nt' or not text:
        return _fallback_sort_bytes(text)
    try:
        import ctypes
        from ctypes import wintypes

        lcmapsortkey = 0x00000400
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        lc_map_string_ex = kernel32.LCMapStringEx
        lc_map_string_ex.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.LPCWSTR,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        lc_map_string_ex.restype = ctypes.c_int
        needed = lc_map_string_ex('zh-CN', lcmapsortkey, text, len(text), None, 0, None, None, None)
        if needed <= 0:
            return _fallback_sort_bytes(text)
        buffer = (ctypes.c_ubyte * needed)()
        written = lc_map_string_ex('zh-CN', lcmapsortkey, text, len(text), ctypes.byref(buffer), needed, None, None, None)
        if written <= 0:
            return _fallback_sort_bytes(text)
        return bytes(buffer)
    except Exception:
        return _fallback_sort_bytes(text)


def _wechat_name_sort_key(value):
    text = _clean_sort_text(value)
    first = text[:1]
    if first.isascii() and first.isdigit():
        group = 0
    elif first.isascii() and first.isalpha():
        group = 1
    elif '\u4e00' <= first <= '\u9fff':
        group = 2
    else:
        group = 3
    return (group, _windows_zh_sort_key(text), text.casefold(), text)


def normalize_voice_reply_config(config):
    config = normalize_tts_settings(config)
    config.setdefault('chat_voice_reply_switch', False)
    config.setdefault('chat_voice_recognition_switch', False)
    trigger_mode_source = config.get('chat_voice_reply_trigger_modes')
    trigger_modes_missing = trigger_mode_source is None
    if trigger_modes_missing:
        trigger_mode_source = ['keyword']
    trigger_modes = [
        mode for mode in _clean_unique_string_list(trigger_mode_source)
        if mode in {'incoming_voice', 'keyword'}
    ]
    if trigger_modes_missing and not trigger_modes:
        trigger_modes = ['keyword']
    config['chat_voice_reply_trigger_modes'] = trigger_modes
    if 'incoming_voice' in trigger_modes:
        config['chat_voice_recognition_switch'] = True
    if 'voice_transcription_fallback_text' not in config:
        config['voice_transcription_fallback_text'] = DEFAULT_VOICE_TRANSCRIPTION_FALLBACK_TEXT
    else:
        config['voice_transcription_fallback_text'] = str(
            config.get('voice_transcription_fallback_text') or ''
        ).strip()
    config.setdefault('voice_transcription_fallback_reply_once', False)
    config['chat_voice_reply_request_keywords'] = _split_inline_keyword_list(
        config.get('chat_voice_reply_request_keywords', DEFAULT_CHAT_VOICE_REPLY_KEYWORDS)
    ) or list(DEFAULT_CHAT_VOICE_REPLY_KEYWORDS)
    config.setdefault('chat_voice_reply_cooldown_minutes', 10)
    config.setdefault('chat_voice_reply_limit_count', 50)
    config.setdefault('chat_voice_reply_limit_hours', 24)
    config.setdefault('chat_voice_session_minutes', 10)
    config.setdefault('chat_voice_session_turns', 5)
    config.setdefault('group_voice_reply_switch', False)
    config.setdefault('group_voice_recognition_switch', False)
    config['group_voice_reply_request_keywords'] = _split_inline_keyword_list(
        config.get('group_voice_reply_request_keywords', DEFAULT_GROUP_VOICE_REPLY_KEYWORDS)
    ) or list(DEFAULT_GROUP_VOICE_REPLY_KEYWORDS)
    config.setdefault('group_voice_reply_cooldown_minutes', 0)
    config.setdefault('group_voice_reply_limit_count', 99)
    config.setdefault('group_voice_reply_limit_hours', 24)
    return config

# 启动时确保目录存在
os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(PANEL_LOG_DIR, exist_ok=True)


def _account_area_dir(wx_id, area, *, create=False, base_dir=None):
    wx_id = str(wx_id or '').strip()
    if not wx_id:
        raise ValueError('wx_id is required')
    root = DATA_DIR if base_dir is None else base_dir
    return str(account_area_dir(root, wx_id, area, create=create))


def _account_memory_dir(wx_id, *, create=False):
    return _account_area_dir(wx_id, 'memory', create=create, base_dir=MEMORY_BASE)


def _account_chat_memory_dir(wx_id, *, create=False):
    return _account_area_dir(wx_id, 'chat_memory', create=create, base_dir=CHAT_MEMORY_BASE)


def _account_contact_profiles_dir(wx_id, *, create=False):
    return _account_area_dir(wx_id, 'contact_profiles', create=create, base_dir=CONTACT_PROFILES_DIR)


def _identity_index_for_wx_id(wx_id):
    return load_identity_index(DATA_DIR, wx_id)


def _save_identity_index_for_wx_id(wx_id, index):
    return save_identity_index(DATA_DIR, wx_id, index)


def _account_moments_drafts_dir(wx_id, *, create=False):
    return _account_area_dir(wx_id, 'moments_drafts', create=create)


def _running_wx_id():
    if bot_thread and bot_thread.is_alive() and bot:
        return str(getattr(bot, 'wx_id', '') or getattr(getattr(bot, 'memory_manager', None), 'wx_id', '') or '').strip()
    return ''


def _read_last_wx_id():
    try:
        with open(LAST_WX_ID_FILE, 'r', encoding='utf-8') as f:
            return str(f.read() or '').strip()
    except Exception:
        return ''


def _write_last_wx_id(wx_id):
    wx_id = str(wx_id or '').strip()
    try:
        with open(LAST_WX_ID_FILE, 'w', encoding='utf-8') as f:
            f.write(wx_id)
    except Exception:
        pass


def _account_scope_context(base_dir=None):
    root = DATA_DIR if base_dir is None else base_dir
    root_abs = os.path.abspath(str(root))
    include_current_accounts = root_abs in {
        os.path.abspath(str(DATA_DIR)),
        os.path.abspath(str(MEMORY_BASE)),
        os.path.abspath(str(CHAT_MEMORY_BASE)),
        os.path.abspath(str(CONTACT_PROFILES_DIR)),
    }
    existing_ids = discover_populated_account_ids(root)
    running = _running_wx_id() if include_current_accounts else ''
    last = _read_last_wx_id() if include_current_accounts else ''
    return root, include_current_accounts, existing_ids, running, last


def _available_account_wx_ids(base_dir=None):
    root, include_current_accounts, existing_ids, running, last = _account_scope_context(base_dir)
    ordered = [wx_id for wx_id in sorted(set(existing_ids)) if wx_id]
    for wx_id in [running, last]:
        wx_id = str(wx_id or '').strip()
        if wx_id and wx_id not in ordered:
            ordered.append(wx_id)
    wx_ids = set(ordered)
    non_default = [wx_id for wx_id in ordered if wx_id != DEFAULT_ACCOUNT_ID]
    if non_default:
        ids = non_default
        if DEFAULT_ACCOUNT_ID in wx_ids:
            ids.append(DEFAULT_ACCOUNT_ID)
        return ids
    if not include_current_accounts:
        return [DEFAULT_ACCOUNT_ID] if DEFAULT_ACCOUNT_ID in existing_ids else []
    ensure_default_account(root)
    return [DEFAULT_ACCOUNT_ID]


def _preferred_account_wx_id(base_dir=None):
    root, _include_current_accounts, existing_ids, running, last = _account_scope_context(base_dir)
    preferred = preferred_account_id(
        running_wx_id=running,
        last_wx_id=last,
        existing_ids=existing_ids,
    )
    if preferred == DEFAULT_ACCOUNT_ID:
        ensure_default_account(root)
    return preferred


def _account_picker_payload(base_dir=None, selected_wx_id=''):
    wx_ids = _available_account_wx_ids(base_dir)
    selected = str(selected_wx_id or '').strip()
    if selected:
        try:
            selected = _validate_known_account_wx_id(selected, base_dir=base_dir)
        except ValueError:
            selected = ''
    selected = selected or _preferred_account_wx_id(base_dir)
    if selected and selected not in wx_ids:
        wx_ids = list(wx_ids) + [selected]
    if not selected and wx_ids:
        selected = wx_ids[0]
    return {
        'wx_ids': wx_ids,
        'wx_id': selected,
    }


def _validate_known_account_wx_id(wx_id, *, base_dir=None):
    candidate = str(wx_id or '').strip()
    if not candidate:
        return ''
    _root, _include_current_accounts, existing_ids, running, last = _account_scope_context(base_dir)
    if is_known_account_id(candidate, running_wx_id=running, last_wx_id=last, existing_ids=existing_ids):
        return candidate
    raise ValueError('所选微信号不存在或已失效，请重新选择')


def _task_scope_wx_id_from_request():
    candidate = str(request.args.get('task_wx_id', '') or '').strip()
    if candidate:
        try:
            return _validate_known_account_wx_id(candidate)
        except ValueError:
            pass
    return _preferred_account_wx_id()


def _task_scope_options(selected_wx_id=''):
    return _account_picker_payload(selected_wx_id=selected_wx_id)


def _validated_task_scope_wx_id(raw_wx_id=''):
    explicit = str(raw_wx_id or '').strip()
    if explicit:
        return _validate_known_account_wx_id(explicit)
    return _preferred_account_wx_id()


def _material_outreach_tasks_file(wx_id, *, create_parent=False):
    wx_id = str(wx_id or '').strip()
    if not wx_id:
        raise ValueError('wx_id is required')
    return str(account_module_file(DATA_DIR, wx_id, 'material_outreach', 'tasks.json', create_parent=create_parent))


def _material_outreach_runtime_file(wx_id, *, create_parent=False):
    wx_id = str(wx_id or '').strip()
    if not wx_id:
        raise ValueError('wx_id is required')
    return str(account_module_file(DATA_DIR, wx_id, 'material_outreach', 'runtime.json', create_parent=create_parent))


def _material_outreach_history_file(wx_id, *, create_parent=False):
    wx_id = str(wx_id or '').strip()
    if not wx_id:
        raise ValueError('wx_id is required')
    return str(account_module_file(DATA_DIR, wx_id, 'material_outreach', 'history.json', create_parent=create_parent))


def _material_outreach_materials_file(wx_id, *, create_parent=False):
    wx_id = str(wx_id or '').strip()
    if not wx_id:
        raise ValueError('wx_id is required')
    return str(account_module_file(DATA_DIR, wx_id, 'material_outreach', 'materials.json', create_parent=create_parent))


def _load_material_outreach_runtime(wx_id=''):
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id()
    if not wx_id:
        return normalize_material_outreach_runtime_payload({})
    return normalize_material_outreach_runtime_payload(load_json_object(_material_outreach_runtime_file(wx_id)))


def _save_material_outreach_runtime(runtime_payload, wx_id=''):
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id()
    if not wx_id:
        raise ValueError('wx_id is required')
    normalized = normalize_material_outreach_runtime_payload(runtime_payload)
    save_json_object(_material_outreach_runtime_file(wx_id, create_parent=True), normalized)
    return normalized


def _load_material_outreach_history(wx_id=''):
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id()
    if not wx_id:
        return normalize_material_outreach_history_payload({})
    return normalize_material_outreach_history_payload(load_json_object(_material_outreach_history_file(wx_id)))


def _save_material_outreach_history(history_payload, wx_id=''):
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id()
    if not wx_id:
        raise ValueError('wx_id is required')
    normalized = normalize_material_outreach_history_payload(history_payload)
    save_json_object(_material_outreach_history_file(wx_id, create_parent=True), normalized)
    return normalized


def _load_material_outreach_materials(wx_id=''):
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id()
    if not wx_id:
        return []
    return load_json_list(_material_outreach_materials_file(wx_id))


def _save_material_outreach_materials(materials, wx_id=''):
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id()
    if not wx_id:
        raise ValueError('wx_id is required')
    normalized = [normalize_material_record(item) for item in (materials or []) if isinstance(item, dict)]
    normalized = [item for item in normalized if item]
    save_json_list(_material_outreach_materials_file(wx_id, create_parent=True), normalized)
    return normalized


def material_outreach_paths_for_wx_id(wx_id=''):
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id()
    if not wx_id:
        base = os.path.join(DATA_DIR, 'accounts')
        return {
            'dir': base,
            'tasks': '',
            'runtime': '',
            'history': '',
            'materials': '',
        }
    module_dir = os.path.dirname(_material_outreach_tasks_file(wx_id, create_parent=False))
    return {
        'dir': module_dir,
        'tasks': _material_outreach_tasks_file(wx_id),
        'runtime': _material_outreach_runtime_file(wx_id),
        'history': _material_outreach_history_file(wx_id),
        'materials': _material_outreach_materials_file(wx_id),
    }


def _material_outreach_wx_id_from_request():
    if request.method == 'GET':
        candidate = request.args.get('wx_id', '')
    elif request.is_json:
        payload = request.get_json(silent=True) or {}
        candidate = payload.get('wx_id', '')
    else:
        candidate = request.form.get('wx_id', '')
    wx_id = str(candidate or '').strip()
    if wx_id:
        return _validate_known_account_wx_id(wx_id)
    return _preferred_account_wx_id()


def _current_material_outreach_wx_id():
    return _material_outreach_wx_id_from_request()


def _current_material_runtime_ids():
    if bot_thread and bot_thread.is_alive() and bot:
        return set(getattr(bot, '_material_runtime_messages', {}) or {})
    return set()


def _normalize_material_copy_note(value):
    return str(value or '').strip()


def build_material_management_view(materials, runtime_material_ids=None):
    runtime_ids = set(runtime_material_ids or [])
    view = []
    for item in materials or []:
        material = normalize_material_record(item)
        if not material:
            continue
        view_item = {
            'id': material['id'],
            'source': material['source'],
            'type': material['type'],
            'type_bucket': material['type_bucket'],
            'content_preview': material['content_preview'],
            'created_at': material['created_at'],
            'status': material['status'],
            'ownership': material['ownership'],
            'copy_note': material['copy_note'],
            'forward_test_status': material['forward_test_status'],
            'last_error': material['last_error'],
            'runtime_available': material['id'] in runtime_ids,
        }
        view.append(view_item)
    return list(reversed(view))


def _material_management_response(wx_id, materials, *, message='素材已更新'):
    history = _load_material_outreach_history(wx_id)
    send_records = history['send_records']
    skip_records = history['skip_records']
    runtime_ids = _current_material_runtime_ids()
    return {
        'status': 'success',
        'message': message,
        'materials': build_material_management_view(materials, runtime_ids),
        'stats': material_outreach_stats(
            materials,
            send_records,
            skip_records,
            runtime_material_ids=runtime_ids,
        ),
    }


def _material_outreach_fixed_material_label(task, materials):
    material_id = str((task or {}).get('fixed_material_id') or '').strip()
    if not material_id:
        return '随机素材'
    for material in materials or []:
        if str((material or {}).get('id') or '').strip() != material_id:
            continue
        return material_display_label(
            (material or {}).get('type_bucket') or (material or {}).get('type') or '',
            (material or {}).get('content_preview') or material_id,
        )
    return '固定素材'


def _materials_by_id(materials):
    result = {}
    for material in materials or []:
        if not isinstance(material, dict):
            continue
        material_id = str((material or {}).get("id") or "").strip()
        if material_id:
            result[material_id] = material
    return result


def build_material_outreach_manual_queue_view(tasks, materials):
    queue = []
    titled_tasks = _apply_material_outreach_display_titles(list(tasks or []), materials)
    for index, task in enumerate(titled_tasks, start=1):
        if not isinstance(task, dict) or not task.get('enabled', True):
            continue
        task_id = str(task.get('id') or task.get('task_id') or '').strip()
        if not task_id:
            continue
        scheduled_at = (
            str(task.get('next_fire_at') or '').strip()
            or str(task.get('execute_after') or '').strip()
            or str(task.get('start_at') or '').strip()
            or str(task.get('fire_at') or '').strip()
            or str(task.get('time') or '').strip()
        )
        queue.append({
            'kind': 'manual',
            'queue_id': f'material:{task_id}',
            'task_id': task_id,
            'chat_name': str(task.get('display_title') or task.get('name') or f'素材转发任务 {index}').strip(),
            'target': str(task.get('display_title') or task.get('name') or f'素材转发任务 {index}').strip(),
            'material': _material_outreach_fixed_material_label(task, materials),
            'scheduled_at': scheduled_at or '等待调度',
            'status': str(task.get('status') or 'pending').strip() or 'pending',
            'status_label': '待执行',
            'can_cancel': True,
        })
    return queue


def _material_outreach_status_response(wx_id, *, message='素材转发状态已更新'):
    config = _inject_account_scoped_task_config(read_config() or {}, wx_id=wx_id)
    tasks = config.get('material_outreach_list', [])
    materials = _load_material_outreach_materials(wx_id)
    history = _load_material_outreach_history(wx_id)
    runtime = _load_material_outreach_runtime(wx_id)
    ai_outreach_config = normalize_ai_material_outreach_config(config)
    send_records = history['send_records']
    skip_records = history['skip_records']
    progress_records = history['progress_records']
    runtime_ids = _current_material_runtime_ids()
    ai_candidate_count = len(
        filter_ai_outreach_candidate_pool(
            build_ai_candidate_material_cards(materials),
            allowed_sources=ai_outreach_config.get('ai_material_outreach_allowed_sources'),
        )
    )
    preface_queue = build_material_outreach_preface_queue_view(runtime['preface_pending_queue'])
    ai_queue = build_ai_material_outreach_queue_view(runtime['ai_pending_queue'])
    preface_pending_count = sum(1 for item in runtime['preface_pending_queue'] if str((item or {}).get('status') or '').strip() == 'pending')
    ai_pending_count = sum(1 for item in runtime['ai_pending_queue'] if str((item or {}).get('status') or '').strip() == 'pending')
    queue = preface_queue + ai_queue
    return {
        'status': 'success',
        'message': message,
        'materials': build_material_management_view(materials, runtime_ids),
        'stats': material_outreach_stats(
            materials,
            send_records,
            skip_records,
            runtime_material_ids=runtime_ids,
        ),
        'browser': build_material_outreach_browser(tasks, progress_records, materials),
        'queue': queue,
        'pending_count': preface_pending_count + ai_pending_count,
        'ai_candidate_count': ai_candidate_count,
        'preface_queue': preface_queue,
        'preface_pending_count': preface_pending_count,
        'ai_queue': ai_queue,
        'ai_pending_count': ai_pending_count,
    }


def _request_material_runtime_reload():
    runtime_bot = globals().get('bot')
    if runtime_bot and hasattr(runtime_bot, 'request_runtime_task_reload'):
        try:
            runtime_bot.request_runtime_task_reload()
        except Exception as exc:
            log('WARNING', f'运行中素材转发任务同步失败，将在下次刷新后生效：{exc}')


def _material_outreach_parse_datetime(value):
    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    normalized = raw_value.replace("/", "-").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return None


def _apply_material_outreach_display_titles(tasks, materials=None):
    materials_by_id = _materials_by_id(materials)
    titled_tasks = []
    for task in tasks or []:
        task_copy = dict(task) if isinstance(task, dict) else {}
        task_copy["display_title"] = material_outreach_task_title(task_copy, materials_by_id=materials_by_id)
        titled_tasks.append(task_copy)
    return titled_tasks


def _apply_scheduled_message_display_titles(tasks):
    titled_tasks = []
    for task in tasks or []:
        task_copy = dict(task) if isinstance(task, dict) else {}
        task_copy["display_title"] = scheduled_message_task_title(task_copy)
        titled_tasks.append(task_copy)
    return titled_tasks


def _apply_moments_display_titles(tasks):
    titled_tasks = []
    for task in tasks or []:
        task_copy = dict(task) if isinstance(task, dict) else {}
        task_copy["display_title"] = moments_task_title(task_copy)
        titled_tasks.append(task_copy)
    return titled_tasks


def _material_outreach_task_label(task, index, task_id):
    task = task if isinstance(task, dict) else {}
    label = str(task.get('display_title') or '').strip()
    if label:
        return label
    if task_id:
        return f"任务 {index}"
    return f"未命名任务 {index}"


def _material_outreach_record_time_label(raw_value):
    raw_value = str(raw_value or "").strip()
    if not raw_value:
        return "-"
    normalized = raw_value.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            dt = datetime.strptime(normalized, fmt)
            return dt.strftime("%m/%d %H:%M")
        except ValueError:
            continue
    return raw_value[:16] if len(raw_value) > 16 else raw_value


def _material_outreach_record_detail_label(raw_value):
    text = str(raw_value or "").strip()
    if not text:
        return "-"
    replacements = {
        "没有可用素材": "无可用素材",
        "没有可用素材可发送": "无可用素材",
        "未在通讯录建档中找到该好友": "未建档",
        "目标处于冷却期": "冷却中",
        "目标冷却中，暂不发送": "冷却中",
        "所有素材已失效": "素材已失效",
    }
    if text in replacements:
        return replacements[text]
    if len(text) > 18:
        return text[:18].rstrip() + "…"
    return text


def _material_outreach_record_material_label(material_type, material_title):
    return material_display_label(material_type, material_title)


def _material_outreach_record_preview(record):
    record = record if isinstance(record, dict) else {}
    material_title = str(record.get('material_title') or '').strip()
    material_type = str(record.get('material_type') or '').strip()
    detail = str(record.get('detail') or '').strip()
    return {
        "time": _material_outreach_record_time_label(record.get('created_at') or record.get('time') or ''),
        "target": str(record.get('display_name') or record.get('send_name') or record.get('target') or '').strip(),
        "status": str(record.get('status') or '').strip(),
        "status_label": str(record.get('status_label') or '').strip(),
        "material": _material_outreach_record_material_label(material_type, material_title),
        "detail": _material_outreach_record_detail_label(detail),
    }


def build_material_outreach_browser(tasks, progress_records, materials=None):
    tasks = _apply_material_outreach_display_titles(list(tasks or []), materials)
    progress_records = [item for item in (progress_records or []) if isinstance(item, dict)]

    grouped_records = {}
    for record in progress_records:
        task_id = str(record.get('task_id') or '').strip()
        grouped_records.setdefault(task_id, []).append(record)

    browser_tasks = []
    known_task_ids = set()
    for index, task in enumerate(tasks, start=1):
        task = task if isinstance(task, dict) else {}
        task_id = str(task.get('id') or task.get('task_id') or '').strip()
        known_task_ids.add(task_id)
        task_records = list(grouped_records.get(task_id, []))
        task_records.sort(key=lambda item: str(item.get('created_at') or ''), reverse=True)

        target_map = {}
        for record in task_records:
            target_key = str(record.get('contact_key') or record.get('send_name') or record.get('display_name') or '').strip()
            if not target_key:
                continue
            target_entry = target_map.setdefault(
                target_key,
                {
                    "target_key": target_key,
                    "label": str(record.get('display_name') or record.get('send_name') or '').strip() or '未命名目标',
                    "meta": "",
                    "status_label": str(record.get('status_label') or '').strip() or '-',
                    "count": 0,
                    "records": [],
                },
            )
            target_entry["count"] += 1
            target_entry["records"].append(_material_outreach_record_preview(record))
        targets = []
        for target_entry in target_map.values():
            target_entry["meta"] = f"{target_entry['count']} 条"
            target_entry["records"].sort(key=lambda item: item.get("time") or "", reverse=True)
            targets.append(target_entry)
        targets.sort(key=lambda item: ((item["records"][0]["time"] if item["records"] else ""), item["label"]), reverse=True)

        browser_tasks.append(
            {
                "task_id": task_id,
                "label": _material_outreach_task_label(task, index, task_id),
                "meta": f"{len(targets)} 人",
                "enabled": bool(task.get('enabled', True)),
                "targets": targets,
            }
        )

    orphan_index = len(browser_tasks) + 1
    for task_id, task_records in grouped_records.items():
        if task_id in known_task_ids:
            continue
        task_records = list(task_records)
        task_records.sort(key=lambda item: str(item.get('created_at') or ''), reverse=True)
        target_map = {}
        for record in task_records:
            target_key = str(record.get('contact_key') or record.get('send_name') or record.get('display_name') or '').strip()
            if not target_key:
                continue
            target_entry = target_map.setdefault(
                target_key,
                {
                    "target_key": target_key,
                    "label": str(record.get('display_name') or record.get('send_name') or '').strip() or '未命名目标',
                    "meta": "",
                    "status_label": str(record.get('status_label') or '').strip() or '-',
                    "count": 0,
                    "records": [],
                },
            )
            target_entry["count"] += 1
            target_entry["records"].append(_material_outreach_record_preview(record))
        targets = []
        for target_entry in target_map.values():
            target_entry["meta"] = f"{target_entry['count']} 条"
            target_entry["records"].sort(key=lambda item: item.get("time") or "", reverse=True)
            targets.append(target_entry)
        targets.sort(key=lambda item: ((item["records"][0]["time"] if item["records"] else ""), item["label"]), reverse=True)
        browser_tasks.append(
            {
                "task_id": task_id,
                "label": f"任务 {orphan_index}",
                "meta": f"{len(targets)} 人",
                "enabled": True,
                "targets": targets,
            }
        )
        orphan_index += 1

    selected_task_id = browser_tasks[0]["task_id"] if browser_tasks else ""
    selected_target_key = ""
    if browser_tasks and browser_tasks[0]["targets"]:
        selected_target_key = browser_tasks[0]["targets"][0]["target_key"]

    return {
        "tasks": browser_tasks,
        "selected_task_id": selected_task_id,
        "selected_target_key": selected_target_key,
    }


def _delete_material_outreach_task_records(task_id, wx_id=''):
    task_id = str(task_id or '').strip()
    if not task_id:
        raise ValueError('请先选择任务')
    history = _load_material_outreach_history(wx_id)
    removed = {"send_records": 0, "skip_records": 0, "progress_records": 0}
    for key in ("send_records", "skip_records", "progress_records"):
        items = history[key]
        kept = [item for item in items if str((item or {}).get('task_id') or '').strip() != task_id]
        removed[key] = len(items) - len(kept)
        history[key] = kept
    if any(removed.values()):
        _save_material_outreach_history(history, wx_id)
    return removed


def build_material_outreach_preface_queue_view(queue_records):
    items = []
    for record in queue_records or []:
        if not isinstance(record, dict):
            continue
        status = str(record.get('status') or '').strip() or 'pending'
        if status != 'pending':
            continue
        material_label = material_display_label(
            str(record.get('material_type') or '').strip(),
            str(record.get('material_title') or '').strip(),
        )
        detail_parts = [material_label] if material_label else []
        if (
            str(record.get('preface_status') or '').strip().lower() == 'failed'
            and str(record.get('failure_mode') or '').strip() == 'send_without_preface'
        ):
            detail_parts.append('文案失败，改为无文案发送')
            error_text = str(record.get('preface_error') or record.get('error') or '').strip()
            if error_text:
                detail_parts.append(error_text)
        items.append({
            'kind': 'preface',
            'queue_id': str(record.get('queue_id') or '').strip(),
            'status': status,
            'status_label': '待发送' if str(record.get('preface_status') or '').strip().lower() == 'success' else '待预生成',
            'chat_name': str(record.get('display_name') or record.get('target') or '').strip(),
            'target': str(record.get('target') or '').strip(),
            'material': material_label,
            'detail': ' · '.join(part for part in detail_parts if part),
            'scheduled_at': _material_outreach_record_time_label(record.get('scheduled_at') or ''),
            'can_cancel': status == 'pending',
        })
    items.sort(
        key=lambda item: (
            0 if item.get('status') == 'pending' else 1,
            str(item.get('scheduled_at') or ''),
            str(item.get('queue_id') or ''),
        )
    )
    return items


def build_ai_material_outreach_queue_view(queue_records):
    items = []
    for record in queue_records or []:
        if not isinstance(record, dict):
            continue
        status = str(record.get('status') or '').strip() or 'pending'
        if status != 'pending':
            continue
        items.append({
            'queue_id': str(record.get('queue_id') or '').strip(),
            'status': status,
            'status_label': '待发送',
            'chat_name': str(record.get('chat_name') or record.get('target') or '').strip(),
            'target': str(record.get('target') or '').strip(),
            'material': material_display_label(
                str(record.get('material_type') or '').strip(),
                str(record.get('material_title') or '').strip(),
            ),
            'scheduled_at': _material_outreach_record_time_label(record.get('scheduled_at') or ''),
            'can_cancel': status == 'pending',
        })
    items.sort(
        key=lambda item: (
            0 if item.get('status') == 'pending' else 1,
            str(item.get('scheduled_at') or ''),
            str(item.get('queue_id') or ''),
        )
    )
    return items


def load_email_config(path=EMAIL_FILE):
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return dict(DEFAULT_EMAIL_CONFIG)
    if not isinstance(data, dict):
        return dict(DEFAULT_EMAIL_CONFIG)
    return {
        "host": str(data.get("host", "") or ""),
        "port": data.get("port", ""),
        "user": str(data.get("user", "") or ""),
        "pass": str(data.get("pass", "") or ""),
    }


def save_email_config_file(config, path=EMAIL_FILE):
    config = config if isinstance(config, dict) else {}
    normalized = {
        "host": str(config.get("host", "") or "").strip(),
        "port": int(str(config.get("port", "") or "0").strip()),
        "user": str(config.get("user", "") or "").strip(),
        "pass": str(config.get("pass", "") or "").strip(),
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(normalized, file, ensure_ascii=False, indent=4)
    return normalized


def load_panel_secret_key():
    """读取或生成持久化 Flask 会话密钥。"""
    if os.path.exists(APP_SECRET_FILE):
        try:
            with open(APP_SECRET_FILE, 'r', encoding='utf-8') as f:
                secret = f.read().strip()
            if secret:
                return secret
        except Exception as e:
            log('WARNING', f'读取面板会话密钥失败，将重新生成: {e}')

    secret = secrets.token_urlsafe(64)
    try:
        with open(APP_SECRET_FILE, 'w', encoding='utf-8') as f:
            f.write(secret)
    except Exception as e:
        log('ERROR', f'写入面板会话密钥失败，当前会话将使用临时密钥: {e}')
    return secret


app.secret_key = load_panel_secret_key()


def hash_password(password):
    return generate_password_hash(password, method='pbkdf2:sha256', salt_length=16)


def verify_password(password, password_hash):
    try:
        return check_password_hash(password_hash, password)
    except Exception:
        return False


def load_siver_panel_manager_class():
    try:
        from extension.siver_panel import SiverPanelManager
        return SiverPanelManager
    except Exception as e:
        log('ERROR', f'加载 SiverPanel 客户端模块失败: {e}')
        return None

def load_admin_credentials():
    """从 admin.json 读取账密，文件不存在时自动创建默认账密文件"""
    default_password = "123456"
    default = {"username": "admin", "password_hash": hash_password(default_password)}
    if not os.path.exists(ADMIN_FILE):
        try:
            with open(ADMIN_FILE, 'w', encoding='utf-8') as f:
                json.dump(default, f, ensure_ascii=False, indent=4)
            log('WARNING', f'账密文件不存在，已创建默认账密文件: {ADMIN_FILE}，请及时修改密码')
        except Exception as e:
            log('ERROR', f'创建账密文件失败: {e}，使用默认账密')
        return default
    try:
        with open(ADMIN_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        username = data.get("username", default["username"])
        password_hash = str(data.get("password_hash", "")).strip()

        if not password_hash:
            password_hash = default["password_hash"]

        return {
            "username": username,
            "password_hash": password_hash,
        }
    except Exception as e:
        log('ERROR', f'读取账密文件失败: {e}，使用默认账密')
        return default

# 用户认证信息（从 admin.json 加载）
USERS = load_admin_credentials()

LOGIN_FAIL_LIMIT = 8
LOGIN_FAIL_WINDOW_SEC = 15 * 60
LOGIN_BAN_SEC = 30 * 60
login_failures = defaultdict(deque)
login_bans = {}
panel_server_port = None
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "123456"
FORCE_ADMIN_CHANGE_ALLOWED_PATHS = {
    "/dashboard",
    "/logout",
    "/api/check_auth",
    "/get_admin_config",
    "/save_admin_config",
}


def get_client_ip():
    forwarded_for = request.headers.get('X-Forwarded-For', '')
    if forwarded_for:
        return forwarded_for.split(',')[0].strip()
    real_ip = request.headers.get('X-Real-IP', '').strip()
    if real_ip:
        return real_ip
    return request.remote_addr or 'unknown'


def is_remote_panel_request():
    if request.headers.get('X-Siver-Remote', '').strip() == '1':
        return True
    forwarded_prefix = request.headers.get('X-Forwarded-Prefix', '').strip()
    return forwarded_prefix.startswith('/panel/')


def is_default_admin_credentials():
    return (
        USERS.get("username") == DEFAULT_ADMIN_USERNAME
        and verify_password(DEFAULT_ADMIN_PASSWORD, USERS.get("password_hash", ""))
    )


def is_force_admin_change_required():
    if not session.get('logged_in'):
        return False
    if not is_remote_panel_request():
        return False
    return is_default_admin_credentials()


def get_remote_connect_block_reason(*, manual: bool) -> tuple[str, str] | None:
    if not is_default_admin_credentials():
        return None
    message = '当前后台仍在使用默认账号密码 admin / 123456。为安全起见，请先在“账号密码”里修改后台账号密码后，再连接远程访问服务。'
    log('WARNING', message)
    return ('default_admin_credentials_block_remote_connect', message)


def is_remote_connect_block_required():
    config = read_config() or {}
    return bool(config.get('siver_panel_enabled') and is_default_admin_credentials())


def is_login_ip_banned(ip):
    expire_ts = login_bans.get(ip)
    if not expire_ts:
        return False, 0
    now = time.time()
    if expire_ts <= now:
        login_bans.pop(ip, None)
        return False, 0
    return True, int(expire_ts - now)


def record_login_failure(ip):
    now = time.time()
    bucket = login_failures[ip]
    while bucket and now - bucket[0] > LOGIN_FAIL_WINDOW_SEC:
        bucket.popleft()
    bucket.append(now)
    if len(bucket) >= LOGIN_FAIL_LIMIT:
        login_bans[ip] = now + LOGIN_BAN_SEC
        bucket.clear()
        return True
    return False


def clear_login_failures(ip):
    login_failures.pop(ip, None)
    login_bans.pop(ip, None)


def is_safe_redirect_target(target):
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc


def absolute_url_for(endpoint, **values):
    return url_for(endpoint, _external=True, **values)


@app.after_request
def apply_panel_security_headers(response):
    session_cookie_name = app.config.get('SESSION_COOKIE_NAME', 'session')
    cookies = response.headers.getlist('Set-Cookie')
    if request.is_secure and cookies:
        rewritten = []
        changed = False
        for cookie in cookies:
            if cookie.startswith(f'{session_cookie_name}=') and 'Secure' not in cookie:
                cookie = f'{cookie}; Secure'
                changed = True
            rewritten.append(cookie)
        if changed:
            del response.headers['Set-Cookie']
            for cookie in rewritten:
                response.headers.add('Set-Cookie', cookie)
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    return response


def get_panel_server_port():
    return panel_server_port


SIVER_PANEL_MANAGER_CLASS = load_siver_panel_manager_class()
siver_panel_manager = None
if SIVER_PANEL_MANAGER_CLASS is not None:
    try:
        siver_panel_manager = SIVER_PANEL_MANAGER_CLASS(
            config_path=CONFIG_FILE,
            client_version=BOT_VERSION,
            log_func=log,
        )
        siver_panel_manager.set_connect_guard(get_remote_connect_block_reason)
    except Exception as e:
        log('ERROR', f'初始化 SiverPanel 客户端失败: {e}')

if siver_panel_manager is not None:
    atexit.register(siver_panel_manager.shutdown)

# 日志颜色映射
LOG_COLORS = {
    'INFO': 'text-primary',
    'WARNING': 'text-warning',
    'ERROR': 'text-danger',
    'DEBUG': 'text-secondary',
    'SUCCESS': 'text-success'
}

log_messages = []

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/') or request.accept_mimetypes.accept_json:
                return jsonify({'status': 'error', 'message': '未登录'}), 401
            return redirect(absolute_url_for('login', next=request.url))
        if is_force_admin_change_required() and request.path not in FORCE_ADMIN_CHANGE_ALLOWED_PATHS:
            message = '当前为远程访问，且仍在使用默认账号密码，请先修改后台账号密码后再继续使用'
            wants_json = (
                request.path.startswith('/api/')
                or request.accept_mimetypes.accept_json
                or request.headers.get('X-Requested-With', '') == 'XMLHttpRequest'
            )
            if wants_json:
                return jsonify({
                    'status': 'error',
                    'message': message,
                    'error_code': 'force_admin_credential_change_required',
                }), 403
            return redirect(absolute_url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

def log_server(level, msg):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = {
        'time': timestamp,
        'level': level,
        'message': msg,
        'color': LOG_COLORS.get(level.upper(), 'text-dark')
    }
    log_messages.append(log_entry)
    if len(log_messages) > 1000:
        log_messages.pop(0)
    print(f"[{timestamp}] [{level}] {msg}")

# ----------------------------------------------------------
# Prompt 文件管理辅助函数
# ----------------------------------------------------------

def _is_persona_status_filename(filename):
    return str(filename or '').endswith(f'{PERSONA_STATUS_SUFFIX}.md')

def _is_persona_status_name(name):
    return str(name or '').strip().endswith(PERSONA_STATUS_SUFFIX)

def _normalize_prompt_file_name(name):
    name = str(name or '').strip()
    if name.lower().endswith('.md'):
        name = name[:-3].strip()
    return name

def _is_valid_prompt_file_name(name):
    return bool(re.fullmatch(r'[\u4e00-\u9fff\w\s\-]+', str(name or '')))

def _persona_status_path(prompt_name):
    prompt_name = _normalize_prompt_file_name(prompt_name)
    return os.path.join(PROMPT_DIR, f'{prompt_name}{PERSONA_STATUS_SUFFIX}.md')

def _get_default_persona_status_content():
    template_path = os.path.join(PROMPT_DIR, DEFAULT_PERSONA_STATUS_TEMPLATE_NAME)
    try:
        with open(template_path, 'r', encoding='utf-8') as f:
            template_content = f.read()
        if template_content.strip():
            return template_content
    except FileNotFoundError:
        pass
    except Exception as e:
        log('WARNING', f'读取默认人设近况模板失败，将回退内置模板：{e}')
    return DEFAULT_PERSONA_STATUS_CONTENT

def _persona_status_has_usable_items(content):
    text = str(content or '')
    match = PERSONA_STATUS_AVAILABLE_SECTION_RE.search(text)
    if not match:
        return False

    section = match.group(1)
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        normalized = re.sub(r'^[-*+]\s*', '', line)
        normalized = re.sub(r'^\d+[.)]\s*', '', normalized)
        normalized = normalized.strip()
        if normalized and normalized not in PERSONA_STATUS_PLACEHOLDER_LINES:
            return True
    return False

def _ensure_prompt_dir():
    """确保 prompt 目录存在，若为空则创建默认 prompt 文件"""
    os.makedirs(PROMPT_DIR, exist_ok=True)
    try:
        md_files = [
            f for f in os.listdir(PROMPT_DIR)
            if f.endswith('.md') and not _is_persona_status_filename(f)
        ]
    except Exception:
        md_files = []
    if not md_files:
        try:
            with open(os.path.join(PROMPT_DIR, '默认.md'), 'w', encoding='utf-8') as f:
                f.write(DEFAULT_PROMPT_CONTENT)
        except Exception as e:
            log('ERROR', f'创建默认 prompt 文件失败: {e}')

def _get_prompts_list():
    """扫描 PROMPT_DIR，返回 [{name, content}]，"默认" 排第一"""
    _ensure_prompt_dir()
    prompts = []
    try:
        for fname in os.listdir(PROMPT_DIR):
            if not fname.endswith('.md'):
                continue
            if _is_persona_status_filename(fname):
                continue
            name = fname[:-3]
            try:
                with open(os.path.join(PROMPT_DIR, fname), 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception:
                content = ''
            prompts.append({'name': name, 'content': content})
    except Exception as e:
        log('ERROR', f'扫描 prompt 目录失败: {e}')
    # "默认" 排第一，其余字典序
    prompts.sort(key=lambda p: (0 if p['name'] == '默认' else 1, p['name']))
    return prompts

# ----------------------------------------------------------
# 数据备份辅助函数
# ----------------------------------------------------------

def _backup_name_sort_key(name):
    match = re.fullmatch(r'data_(\d{1,2})月(\d{1,2})日 (\d{2})：(\d{2})：(\d{2})(?:_(\d{2}))?', name)
    if not match:
        return None
    month, day, hour, minute, second, suffix = match.groups()
    return (
        int(month),
        int(day),
        int(hour),
        int(minute),
        int(second),
        int(suffix or 0),
    )


def _backup_dir_timestamp():
    now = datetime.now()
    return f'data_{now.month}月{now.day}日 {now.strftime("%H")}：{now.strftime("%M")}：{now.strftime("%S")}'


def _backup_dir_mtime(name):
    try:
        return os.path.getmtime(os.path.join(BACKUP_BASE, name))
    except OSError:
        return float('-inf')


def _next_backup_dir(timestamp):
    candidate = os.path.join(BACKUP_BASE, timestamp)
    if not os.path.exists(candidate):
        return candidate
    index = 2
    while True:
        candidate = os.path.join(BACKUP_BASE, f'{timestamp}_{index:02d}')
        if not os.path.exists(candidate):
            return candidate
        index += 1


def _do_backup():
    """
    执行一次完整数据备份：
      - 将 data/ 完整复制到 backups/data_<时间戳>
      - 在备份目录内创建以当前版本号命名的空标记文件（如 V4.6.10）
    返回备份目录的绝对路径。
    """
    ts = _backup_dir_timestamp()
    backup_dir = _next_backup_dir(ts)
    if os.path.exists(DATA_DIR):
        shutil.copytree(DATA_DIR, backup_dir)
    else:
        os.makedirs(backup_dir, exist_ok=True)

    # 创建版本号标记文件（空文件，文件名即版本号）
    version_marker = os.path.join(backup_dir, BOT_VERSION)
    try:
        open(version_marker, 'w').close()
    except Exception:
        pass

    log('SUCCESS', f'数据已备份至: {backup_dir}')
    return backup_dir


def _check_and_auto_backup():
    """
    启动时自动检查并决定是否需要备份：
      - 首次运行（backups 不存在）且存在 data/ → 立即备份
      - 已有备份但最新一次距今超过 3 天 → 自动备份
      - 最新备份的版本号标记文件与当前版本不一致 → 自动备份
    """
    has_data = os.path.exists(DATA_DIR) and bool(os.listdir(DATA_DIR))
    if not has_data:
        return  # 没有任何数据，无需备份

    if not os.path.exists(BACKUP_BASE):
        log('INFO', '首次检测到数据目录，自动备份中...')
        _do_backup()
        return

    # 找所有备份目录（data_5月25日 14：30：00 或同秒重复时的 data_5月25日 14：30：00_02）
    try:
        backups = [
            d for d in os.listdir(BACKUP_BASE)
            if os.path.isdir(os.path.join(BACKUP_BASE, d))
            and _backup_name_sort_key(d) is not None
        ]
    except Exception:
        backups = []

    if not backups:
        log('INFO', '备份目录为空，执行首次自动备份...')
        _do_backup()
        return

    latest = max(backups, key=lambda name: (_backup_dir_mtime(name), _backup_name_sort_key(name)))

    # 判断距上次备份天数
    try:
        latest_dt = datetime.fromtimestamp(_backup_dir_mtime(latest))
        days_diff = (datetime.now() - latest_dt).days
    except Exception:
        days_diff = 999  # 解析失败时强制备份

    # 判断最新备份是否包含当前版本号标记文件
    latest_path = os.path.join(BACKUP_BASE, latest)
    version_match = os.path.exists(os.path.join(latest_path, BOT_VERSION))

    if days_diff > 3:
        log('INFO', f'距上次备份已 {days_diff} 天（超过3天），自动备份中...')
        _do_backup()
    elif not version_match:
        # 找出实际存储的旧版本号（遍历目录内不含 / 的文件）
        try:
            old_ver_files = [f for f in os.listdir(latest_path)
                             if os.path.isfile(os.path.join(latest_path, f))
                             and f.startswith('V')]
            old_ver = old_ver_files[0] if old_ver_files else '未知版本'
        except Exception:
            old_ver = '未知版本'
        log('INFO', f'检测到版本变更（{old_ver} → {BOT_VERSION}），自动备份中...')
        _do_backup()

# 读取配置文件
def read_config():
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8-sig') as f:
            return json.load(f)
    except Exception as e:
        log('ERROR', f'读取配置文件失败: {str(e)}')
        return None


def _load_json_object(path):
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_json_object(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload if isinstance(payload, dict) else {}, f, ensure_ascii=False, indent=4)


def _scheduled_message_tasks_file(wx_id, *, create_parent=False):
    wx_id = str(wx_id or '').strip()
    if not wx_id:
        raise ValueError('wx_id is required')
    return str(account_module_file(DATA_DIR, wx_id, 'scheduled_message', 'tasks.json', create_parent=create_parent))


def _scheduled_message_runtime_file(wx_id, *, create_parent=False):
    wx_id = str(wx_id or '').strip()
    if not wx_id:
        raise ValueError('wx_id is required')
    return str(account_module_file(DATA_DIR, wx_id, 'scheduled_message', 'runtime.json', create_parent=create_parent))


def _scheduled_message_history_file(wx_id, *, create_parent=False):
    wx_id = str(wx_id or '').strip()
    if not wx_id:
        raise ValueError('wx_id is required')
    return str(account_module_file(DATA_DIR, wx_id, 'scheduled_message', 'history.json', create_parent=create_parent))


def _load_account_scoped_scheduled_message_tasks(wx_id=''):
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id()
    if not wx_id:
        return []
    definitions = load_json_list(_scheduled_message_tasks_file(wx_id))
    runtime_map = _load_json_object(_scheduled_message_runtime_file(wx_id))
    history_map = _load_json_object(_scheduled_message_history_file(wx_id))
    return deserialize_scheduled_message_task_collection(definitions, runtime_map, history_map)


def _save_account_scoped_scheduled_message_tasks(tasks, wx_id=''):
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id()
    if not wx_id:
        raise ValueError('wx_id is required')
    normalized = [
        _normalize_scheduled_message_task_for_persistence(task)
        for task in (tasks or [])
        if isinstance(task, dict)
    ]
    definitions, runtime_map, history_map = serialize_scheduled_message_task_collection(normalized)
    save_json_list(_scheduled_message_tasks_file(wx_id, create_parent=True), definitions)
    _save_json_object(_scheduled_message_runtime_file(wx_id, create_parent=True), runtime_map)
    _save_json_object(_scheduled_message_history_file(wx_id, create_parent=True), history_map)
    return normalized


def _cancel_disabled_scheduled_message_runtime_instances(tasks, wx_id=''):
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id()
    if not wx_id:
        return {}
    runtime_file = _scheduled_message_runtime_file(wx_id, create_parent=False)
    runtime_map = _load_json_object(runtime_file)
    runtime_map = dict(runtime_map) if isinstance(runtime_map, dict) else {}
    if not runtime_map:
        return runtime_map
    disabled_task_ids = {
        str(task.get('id') or '').strip()
        for task in (tasks or [])
        if isinstance(task, dict)
        and not task.get('enabled', True)
        and str(task.get('id') or '').strip()
    }
    if not disabled_task_ids:
        return runtime_map
    changed = False
    for task_id in disabled_task_ids:
        runtime_record = runtime_map.get(task_id)
        if not isinstance(runtime_record, dict):
            continue
        status = str(runtime_record.get('status') or '').strip()
        if status not in {STATUS_PENDING_CONFIRM, STATUS_PENDING}:
            continue
        runtime_map.pop(task_id, None)
        changed = True
    if changed:
        _save_json_object(_scheduled_message_runtime_file(wx_id, create_parent=True), runtime_map)
    return runtime_map


def _scheduled_message_wx_id_from_request():
    if request.method == 'GET':
        candidate = request.args.get('wx_id', '')
    elif request.is_json:
        payload = request.get_json(silent=True) or {}
        candidate = payload.get('wx_id', '')
    else:
        candidate = request.form.get('wx_id', '')
    wx_id = str(candidate or '').strip()
    if wx_id:
        return _validate_known_account_wx_id(wx_id)
    return _preferred_account_wx_id()


def _find_scheduled_message_task(tasks, task_id):
    task_id = str(task_id or '').strip()
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if str(task.get('id') or '').strip() == task_id:
            return task
    return None


def _request_scheduled_message_runtime_reload():
    runtime_bot = globals().get('bot')
    if runtime_bot and hasattr(runtime_bot, 'request_runtime_task_reload'):
        try:
            runtime_bot.request_runtime_task_reload()
        except Exception as exc:
            log('WARNING', f'运行中定时消息任务同步失败，将在下次刷新后生效：{exc}')


def _load_account_scoped_material_outreach_tasks(wx_id=''):
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id()
    if not wx_id:
        return []
    config = {'material_outreach_list': load_json_list(_material_outreach_tasks_file(wx_id))}
    _normalize_schedule_task_lists(config)
    return [task for task in config.get('material_outreach_list', []) if isinstance(task, dict)]


def _save_account_scoped_material_outreach_tasks(tasks, wx_id=''):
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id()
    if not wx_id:
        raise ValueError('wx_id is required')
    config = {'material_outreach_list': [task for task in (tasks or []) if isinstance(task, dict)]}
    _normalize_schedule_task_lists(config)
    normalized = [task for task in config.get('material_outreach_list', []) if isinstance(task, dict)]
    save_json_list(_material_outreach_tasks_file(wx_id, create_parent=True), normalized)
    _cancel_disabled_material_outreach_runtime_instances(normalized, wx_id)
    return normalized


def _keyword_rules_file(wx_id, *, create_parent=False):
    wx_id = str(wx_id or '').strip()
    if not wx_id:
        raise ValueError('wx_id is required')
    return str(account_module_file(DATA_DIR, wx_id, 'keyword_reply', 'rules.json', create_parent=create_parent))


def _normalize_keyword_rule_value(keyword, reply):
    return normalize_keyword_reply_rule(keyword, reply)


def _keyword_rule_display_value(keyword, reply):
    rule = normalize_keyword_reply_rule(keyword, reply) or {'keywords': [], 'text': '', 'files': []}
    return {
        'keyword_input': join_keyword_terms(rule.get('keywords', [])),
        'keywords': rule.get('keywords', []),
        'text': rule.get('text', ''),
        'files': rule.get('files', []),
    }


def _validate_keyword_rules_have_content(rules):
    if not isinstance(rules, dict):
        return
    for key, rule in rules.items():
        if not isinstance(rule, dict):
            continue
        text = str(rule.get('text') or '').strip()
        files = [str(path or '').strip() for path in (rule.get('files') or []) if str(path or '').strip()]
        if not text and not files:
            raise ValueError(f'关键词回复规则必须填写文案或至少一个文件路径：{key}')


def _load_account_scoped_keyword_rules(wx_id=''):
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id()
    if not wx_id:
        return {}
    try:
        with open(_keyword_rules_file(wx_id), 'r', encoding='utf-8-sig') as f:
            rules = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log('ERROR', f'读取关键词回复规则失败: {e}')
        return {}
    config = {'keyword_dict': rules}
    _coerce_dict_fields(config)
    return config.get('keyword_dict', {})


def _save_account_scoped_keyword_rules(rules, wx_id=''):
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id()
    if not wx_id:
        raise ValueError('wx_id is required')
    config = {'keyword_dict': rules}
    _coerce_dict_fields(config)
    with open(_keyword_rules_file(wx_id, create_parent=True), 'w', encoding='utf-8') as f:
        json.dump(config.get('keyword_dict', {}), f, ensure_ascii=False, indent=4)
    return config.get('keyword_dict', {})


def _custom_forward_rules_file(wx_id, *, create_parent=False):
    wx_id = str(wx_id or '').strip()
    if not wx_id:
        raise ValueError('wx_id is required')
    return str(account_module_file(DATA_DIR, wx_id, 'custom_forward', 'rules.json', create_parent=create_parent))


def _load_account_scoped_custom_forward_rules(wx_id=''):
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id()
    if not wx_id:
        return []
    rules = load_json_list(_custom_forward_rules_file(wx_id))
    config = {'custom_forward_list': rules}
    _normalize_custom_forward_rules(config)
    return [rule for rule in config.get('custom_forward_list', []) if isinstance(rule, dict)]


def _save_account_scoped_custom_forward_rules(rules, wx_id=''):
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id()
    if not wx_id:
        raise ValueError('wx_id is required')
    config = {'custom_forward_list': rules}
    _normalize_custom_forward_rules(config)
    normalized = [rule for rule in config.get('custom_forward_list', []) if isinstance(rule, dict)]
    save_json_list(_custom_forward_rules_file(wx_id, create_parent=True), normalized)
    return normalized


def _inject_account_scoped_task_config(config, *, wx_id=''):
    if not isinstance(config, dict):
        return config
    merged = dict(config)
    merged['keyword_dict'] = _load_account_scoped_keyword_rules(wx_id)
    merged['custom_forward_list'] = _load_account_scoped_custom_forward_rules(wx_id)
    merged['scheduled_message_task_list'] = _load_account_scoped_scheduled_message_tasks(wx_id)
    merged['material_outreach_list'] = _load_account_scoped_material_outreach_tasks(wx_id)
    merged['moments_task_list'] = _load_moments_tasks(wx_id)
    return merged

def _parse_hhmm_config(value, field_name):
    """解析 `HH:MM` 格式的时间字段，非法时返回错误信息而不是抛异常。"""
    value = str(value or '').strip()
    if not value:
        return None, f'{field_name} 为空'
    try:
        parsed = datetime.strptime(value, "%H:%M")
        return (parsed.hour, parsed.minute), None
    except ValueError:
        return None, f'{field_name} 格式无效: {value}，应为 HH:MM'

def _normalize_schedule_task_lists(config):
    if not isinstance(config, dict):
        return config

    def _normalize_material_task_for_current_spec(task):
        return _normalize_material_outreach_task_for_persistence(task)

    config['scheduled_message_task_list'] = [
        _normalize_scheduled_message_task_for_persistence(task)
        for task in config.get('scheduled_message_task_list', [])
        if isinstance(task, dict)
    ]
    normalized_material_tasks = []
    for task in config.get('material_outreach_list', []):
        if not isinstance(task, dict):
            continue
        normalized_material_tasks.append(_normalize_material_task_for_current_spec(task))
    config['material_outreach_list'] = normalized_material_tasks
    return config


def _validate_scheduled_message_tasks_have_content(tasks):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        normalized = normalize_scheduled_message_task_payload(task)
        if not normalized.get('enabled', True):
            continue
        msgs = [str(item or '').strip() for item in (normalized.get('msgs') or []) if str(item or '').strip()]
        if msgs:
            continue
        task_name = str(normalized.get('name') or normalized.get('id') or '未命名任务').strip() or '未命名任务'
        raise ValueError(f'定时消息任务至少填写 1 条发送内容：{task_name}')


def _validate_ai_material_outreach_config(config):
    return


def _drop_legacy_ai_material_outreach_fields(config):
    config = config if isinstance(config, dict) else {}
    for field in (
        'ai_material_outreach_allowed_types',
        'ai_material_outreach_miss_throttle_minutes',
        'ai_material_outreach_in_chat_detection_interval_minutes',
        'ai_material_outreach_in_chat_detection_message_threshold',
        'ai_material_outreach_scheduled_scan_switch',
        'ai_material_outreach_scheduled_scan_interval_minutes',
        'ai_material_outreach_scheduled_scan_window_start',
        'ai_material_outreach_scheduled_scan_window_end',
        'ai_material_outreach_scheduled_scan_tags',
        'ai_material_outreach_scheduled_scan_batch_size',
    ):
        config.pop(field, None)
    return config


def _normalize_scheduled_message_task_for_persistence(task):
    task = normalize_scheduled_message_task_payload(task)
    if task.get('enabled', True):
        current_status = str(task.get('status') or '').strip()
        if current_status in {STATUS_PENDING, STATUS_RUNNING} and str(task.get('next_run_at') or '').strip():
            return task
        task = normalize_scheduled_message_task_payload(
            {
                **task,
                'status': STATUS_PENDING,
                'next_run_at': '',
                'current_run_id': '',
                'run_started_at': '',
                'last_result': {},
                'return_reason': '',
                'stop_requested': False,
            }
        )
        return ensure_scheduled_message_next_run(task, now=datetime.now())
    return normalize_scheduled_message_task_payload(
        {
            **task,
            'status': 'pending_confirm',
            'next_run_at': '',
            'current_run_id': '',
            'run_started_at': '',
            'last_result': {},
            'return_reason': '',
            'stop_requested': False,
        }
    )


def _normalize_material_outreach_task_for_persistence(task):
    normalized = normalize_material_outreach_task(task)
    status = str(normalized.get('status') or '').strip().lower()
    has_pending_instance = bool(
        str(normalized.get('next_fire_at') or '').strip()
        or str(normalized.get('execute_after') or '').strip()
        or status == 'pending'
    )
    if not normalized.get('enabled', True) and has_pending_instance:
        normalized['status'] = ''
        normalized['next_fire_at'] = ''
        normalized['execute_after'] = ''
        normalized['last_error'] = ''
    return normalized


def _cancel_disabled_material_outreach_runtime_instances(tasks, wx_id=''):
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id()
    if not wx_id:
        return {}
    active_task_ids = {
        str(task.get('id') or task.get('task_id') or '').strip()
        for task in (tasks or [])
        if isinstance(task, dict)
        and task.get('enabled', True)
        and str(task.get('id') or task.get('task_id') or '').strip()
    }
    cancellable_task_ids = {
        str(task.get('id') or task.get('task_id') or '').strip()
        for task in (tasks or [])
        if isinstance(task, dict)
        and not task.get('enabled', True)
        and str(task.get('id') or task.get('task_id') or '').strip()
    }
    runtime_file = _material_outreach_runtime_file(wx_id, create_parent=False)
    runtime = normalize_material_outreach_runtime_payload(load_json_object(runtime_file))
    for record in runtime.get('preface_pending_queue', []):
        task_id = str(record.get('task_id') or '').strip()
        if task_id and task_id not in active_task_ids:
            cancellable_task_ids.add(task_id)
    for record in runtime.get('ai_pending_queue', []):
        task_id = str(record.get('task_id') or '').strip()
        if task_id and task_id not in active_task_ids:
            cancellable_task_ids.add(task_id)
    if not cancellable_task_ids:
        return runtime
    changed = False
    preface_queue = list(runtime.get('preface_pending_queue') or [])
    kept_preface_queue = [
        record
        for record in preface_queue
        if not (
            str(record.get('task_id') or '').strip() in cancellable_task_ids
            and str(record.get('status') or '').strip() == 'pending'
        )
    ]
    if len(kept_preface_queue) != len(preface_queue):
        runtime['preface_pending_queue'] = kept_preface_queue
        changed = True
    ai_queue = list(runtime.get('ai_pending_queue') or [])
    kept_ai_queue = [
        record
        for record in ai_queue
        if not (
            str(record.get('task_id') or '').strip() in cancellable_task_ids
            and str(record.get('status') or '').strip() == 'pending'
        )
    ]
    if len(kept_ai_queue) != len(ai_queue):
        runtime['ai_pending_queue'] = kept_ai_queue
        changed = True
    if changed:
        save_json_object(runtime_file, runtime)
    return runtime


def _build_schedule_dashboard_view(config, *, wx_id=""):
    if not isinstance(config, dict):
        return config

    view = dict(config)
    view['scheduled_message_task_list'] = _apply_scheduled_message_display_titles([
        build_scheduled_message_task_view(task)
        for task in config.get('scheduled_message_task_list', [])
        if isinstance(task, dict)
    ])
    material_views = []
    for task in config.get('material_outreach_list', []):
        if not isinstance(task, dict):
            continue
        task = normalize_material_outreach_task(task)
        trigger_strategy = normalize_trigger_strategy(task.get('trigger_strategy') or task.get('mode') or 'fixed')
        view_task = dict(task)
        view_task.update(normalize_material_outreach_preface_config(task))
        view_task['trigger_strategy'] = trigger_strategy
        view_task['mode'] = trigger_strategy
        start_at = str(task.get('start_at') or '').strip()
        if not start_at and str(view_task.get('repeat_type') or '') == 'once':
            fire_at = str(task.get('fire_at') or '').strip()
            if fire_at:
                start_at = fire_at[:16]
        view_task['start_at'] = start_at
        material_views.append(view_task)
    material_pool = _load_material_outreach_materials(wx_id)
    view['material_outreach_list'] = _apply_material_outreach_display_titles(material_views, material_pool)
    return view

@app.route('/api/check_auth')
def check_auth():
    return jsonify({'authenticated': session.get('logged_in', False)})

# 登录页
@app.route('/', methods=['GET', 'POST'])
def login():
    if session.get('logged_in'):
        return redirect(absolute_url_for('dashboard'))
    logout_success = request.args.get('logout') == 'success'
    error = None

    if request.method == 'POST':
        client_ip = get_client_ip()
        blocked, remaining = is_login_ip_banned(client_ip)
        if blocked:
            log('WARNING', f'登录被拒绝：IP {client_ip} 仍处于封禁期，剩余 {remaining}s')
            return render_template('login.html', error=f'登录失败次数过多，请 {remaining} 秒后再试', logout_success=logout_success)

        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''

        if username == USERS['username'] and verify_password(password, USERS['password_hash']):
            clear_login_failures(client_ip)
            session['logged_in'] = True
            session['username'] = username
            session.permanent = True
            log('SUCCESS', f'用户 {username} 登录成功')
            next_page = request.args.get('next') or absolute_url_for('dashboard')
            if not is_safe_redirect_target(next_page):
                next_page = absolute_url_for('dashboard')
            return redirect(next_page)
        else:
            record_login_failure(client_ip)
            log('WARNING', f'登录失败: 用户名或密码错误 (用户名: {username})')
            return render_template('login.html', error='用户名或密码错误')

    return render_template('login.html', error=error, logout_success=logout_success)

@app.route('/logout')
def logout():
    session.clear()
    session.pop('logged_in', None)
    session.pop('username', None)
    return redirect(absolute_url_for('login'))

# 仪表盘
@app.route('/dashboard')
@login_required
def dashboard():
    task_scope_wx_id = _task_scope_wx_id_from_request()
    task_scope_options = _task_scope_options(task_scope_wx_id)
    task_scope_wx_id = task_scope_options.get('wx_id', '')
    config = _inject_account_scoped_task_config(read_config() or {}, wx_id=task_scope_wx_id)
    if not config:
        return render_template('error.html', message='无法读取配置文件')

    config.setdefault('api_configs', [
        {"sdk": "DusAPI", "key": "", "url": "https://api.dusapi.com", "model": "gpt-5"},
        {"sdk": "DusAPI", "key": "", "url": "https://api.dusapi.com", "model": "claude-sonnet-4-6"},
    ])
    config.setdefault('api_index', 0)
    config.setdefault('api_capability_map', {})
    config.setdefault('backup_chat_api_index', -1)
    config.setdefault('backup_chat_api_failover_threshold', 3)

    # —— 新增字段默认值（关键）——
    config.setdefault('group_api_map', {})                   # 群组专属接口映射
    config.setdefault('group_welcome_random', 1.0)          # 新人欢迎概率
    config.setdefault('chat_keyword_switch', False)          # 私聊关键词开关
    config.setdefault('group_keyword_switch', False)         # 群组关键词开关
    config.setdefault('group_keyword_at_only', False)        # 群聊关键词仅@时回复
    config.setdefault('keyword_dict', {})                    # 关键词字典
    config.setdefault('scheduled_message_task_list', [])     # 统一定时消息任务列表
    config.setdefault('contact_directory_auto_maintenance_switch', False)
    config.setdefault('contact_directory_auto_maintenance_batch_size', 50)
    config.setdefault('contact_directory_auto_maintenance_interval_minutes', 20)
    config.setdefault('contact_directory_auto_maintenance_full_scan_interval_days', 7)
    config.setdefault('contact_directory_auto_maintenance_window_start', '00:00')
    config.setdefault('contact_directory_auto_maintenance_window_end', '23:59')
    config['contact_directory_auto_maintenance_batch_size'] = normalize_auto_maintenance_batch_size(
        config.get('contact_directory_auto_maintenance_batch_size', 50)
    )
    config['contact_directory_auto_maintenance_interval_minutes'] = coerce_auto_maintenance_interval_minutes(
        config.get('contact_directory_auto_maintenance_interval_minutes', 20)
    )
    config['contact_directory_auto_maintenance_full_scan_interval_days'] = coerce_auto_maintenance_full_scan_interval_days(
        config.get('contact_directory_auto_maintenance_full_scan_interval_days', 7)
    )
    config['contact_directory_auto_maintenance_window_start'] = coerce_auto_maintenance_window_time(
        config.get('contact_directory_auto_maintenance_window_start', '00:00'),
        '00:00',
    )
    config['contact_directory_auto_maintenance_window_end'] = coerce_auto_maintenance_window_time(
        config.get('contact_directory_auto_maintenance_window_end', '23:59'),
        '23:59',
    )
    config.setdefault('material_source_list', [])            # 素材投喂联系人
    config.setdefault('material_source_pool_limit_map', {})  # 素材来源 -> 可用池上限
    config.setdefault('material_source_silent', True)        # 素材源非素材消息静默
    config.setdefault('material_outreach_list', [])          # 素材转发任务列表
    config.setdefault('ai_material_outreach_switch', False)
    config.setdefault('ai_material_outreach_allowed_sources', [])
    config.setdefault('ai_material_outreach_sensitivity', 'conservative')
    config.setdefault('ai_material_outreach_daily_limit_per_friend', 3)
    config.setdefault('ai_material_outreach_delay_min_seconds', 10)
    config.setdefault('ai_material_outreach_delay_max_seconds', 30)
    config.setdefault('ai_material_outreach_preface_enabled', True)
    config.setdefault('ai_material_outreach_preface_goal', '日常问候')
    config.setdefault('ai_material_outreach_preface_intensity', '')
    config.setdefault('ai_material_outreach_detection_interval_minutes', 30)
    config.setdefault('ai_material_outreach_detection_message_threshold', 30)
    config.update(normalize_ai_material_outreach_config(config))
    _drop_legacy_ai_material_outreach_fields(config)
    config.setdefault('moments_like_switch', False)          # 随机朋友圈点赞开关
    config.setdefault('moments_like_min', 60)                # 随机点赞最小间隔（分钟）
    config.setdefault('moments_like_max', 120)               # 随机点赞最大间隔（分钟）
    config.setdefault('moments_task_list', [])               # 统一发朋友圈任务列表
    config.setdefault('moments_api_index', 0)                # 发朋友圈专用接口，必须手动选择，默认第一个接口
    config.setdefault('everyday_start_stop_bot_switch', False)
    config.setdefault('everyday_start_bot_time', "08:00")
    config.setdefault('everyday_stop_bot_time', "23:00")
    config.setdefault('memory_switch', True)
    config.setdefault('memory_context_switch', config.get('memory_switch', True))
    config.setdefault('memory_max_count', 5000)
    config.setdefault('memory_context_count', 50)
    config.setdefault('memory_context_assistant_count', 10)
    config.setdefault('reply_delay_switch', True)
    config.setdefault('reply_delay_first_min', 1)
    config.setdefault('reply_delay_first_max', 5)
    config.setdefault('reply_delay_split_min', 1)
    config.setdefault('reply_delay_split_max', 2)
    config.setdefault('reply_delay_split_speed_mode', 'fast')
    config.setdefault('clean_ai_reply_switch', True)
    if isinstance(config.get('material_outreach_list'), list):
        config['material_outreach_list'] = [
            normalize_material_outreach_task(task)
            for task in config['material_outreach_list']
            if isinstance(task, dict)
        ]
    config.setdefault('new_friend_remark_use_nickname', True)
    config.setdefault('new_friend_archive_switch', True)
    config['new_friend_msg'] = normalize_new_friend_welcome_messages(config.get('new_friend_msg'))
    config['new_friend_reply_switch'] = new_friend_welcome_message_has_content(config.get('new_friend_msg'))
    config.setdefault('new_friend_remark_prefix_timestamp', False)
    config.setdefault('new_friend_remark_suffix_timestamp', False)
    config.setdefault('chat_voice_recognition_switch', False)
    config.setdefault('voice_transcription_fallback_reply_once', False)
    config.setdefault('chat_message_merge_delay', 3.0)
    config.setdefault('chat_image_recognition_switch', False)   # 私聊图片识别开关
    config.setdefault('chat_image_recognition_api',    0)        # 私聊识别接口索引
    config.setdefault('group_image_recognition_switch', False)  # 群组图片识别开关
    config.setdefault('group_voice_recognition_switch', False)  # 群组语音转文字开关
    config.setdefault('group_image_recognition_api',   0)        # 群组识别接口索引
    config.setdefault('custom_forward_switch', False)            # 自定义转发总开关
    config.setdefault('custom_forward_list', [])                 # 自定义转发规则列表

    config.setdefault('siver_panel_enabled', False)
    config.setdefault('siver_panel_activation_code', '')
    config.setdefault('siver_panel_activation_code_applied_hash', '')
    config.setdefault('siver_panel_activation_code_failed_hash', '')
    config.setdefault('siver_panel_slug', '')
    config.setdefault('siver_panel_install_id', '')
    config.setdefault('siver_panel_machine_fingerprint', '')
    config.setdefault('siver_panel_device_id', '')
    config.setdefault('siver_panel_device_secret', '')
    config.setdefault('siver_panel_base_url', SIVER_PANEL_BASE_URL)
    config.setdefault('siver_panel_ws_url', SIVER_PANEL_WS_URL)
    config.setdefault('siver_panel_panel_url', '')
    config.setdefault('siver_panel_service_expire_at', '')
    config.setdefault('siver_panel_last_error_code', '')
    config.setdefault('siver_panel_last_error_message', '')

    _ensure_prompt_dir()
    prompts = _get_prompts_list()
    config.setdefault('default_prompt', '默认')
    config.setdefault('listen_list', [])
    config.setdefault('global_blacklist', [])
    config.setdefault('chat_listen_only', False)
    config.setdefault('group_listen_only', False)
    config.setdefault('chat_prompt_map', {})
    config.setdefault('chat_api_map', {})
    config.setdefault('chat_tts_map', {})
    config.setdefault('group_prompt_map', {})
    config.setdefault('chat_memory_switch', True)
    config.setdefault('chat_memory_exclude_list', [])
    config.setdefault('chat_memory_message_threshold', 100)
    config.setdefault('chat_memory_interval_hours', 12)
    config.setdefault('chat_memory_protected_recent_count', 20)
    config.setdefault('api_error_reply', '')               # 接口调用失败时的固定回复，留空=静默
    config.setdefault('api_error_reply_once', False)       # 接口失败固定回复是否同一用户只发一次
    config.setdefault('meta_reply_blocked_reply', '')      # 命中元话术后的固定回复，留空=静默
    config.setdefault('meta_reply_blocked_reply_once', False)  # 命中元话术固定回复是否同一用户只发一次
    config.setdefault('text_reply_limit_switch', False)      # 单用户文本回复次数限制开关
    config.setdefault('text_reply_limit_count', 99)          # 默认最多回复次数
    config.setdefault('text_reply_limit_hours', 24)          # 滚动小时窗口，0=关闭限制
    config.setdefault('text_reply_limit_reply', '')          # 超限后固定话术
    config.setdefault('text_reply_limit_ai_reply', True)     # 超限后AI自动生成结束语
    config.setdefault('text_reply_limit_reply_once', False)  # 超限话术是否同一用户只发一次
    config.setdefault('chat_split_reply_switch', False)   # 私聊拆分多条回复开关
    config.setdefault('chat_split_max_chars', 100)        # 私聊单条最大字数
    config.setdefault('chat_split_max_count', 4)          # 私聊最多条数
    config.setdefault('group_reply_at_msg', True)          # 群聊回复是否@发言人
    config.setdefault('group_reply_quote', True)           # 群聊回复是否引用消息
    config.setdefault('group_split_reply_switch', False)  # 群聊拆分多条回复开关
    config.setdefault('group_split_max_chars', 100)       # 群聊单条最大字数
    config.setdefault('group_split_max_count', 4)         # 群聊最多条数
    normalize_voice_reply_config(config)
    _coerce_backup_chat_api_fields(config)
    _normalize_schedule_task_lists(config)
    dashboard_config = _build_schedule_dashboard_view(config, wx_id=task_scope_wx_id)

    material_wx_id = task_scope_wx_id
    material_pool = _load_material_outreach_materials(material_wx_id)
    material_history = _load_material_outreach_history(material_wx_id)
    material_runtime = _load_material_outreach_runtime(material_wx_id)
    material_send_records = material_history['send_records']
    material_skip_records = material_history['skip_records']
    material_progress_records = material_history['progress_records']
    preface_pending_queue = material_runtime['preface_pending_queue']
    ai_pending_queue = material_runtime['ai_pending_queue']
    runtime_material_ids = _current_material_runtime_ids()
    material_outreach_status = {
        "materials": build_material_management_view(material_pool, runtime_material_ids),
        "send_records": list(reversed(material_send_records[-20:])),
        "skip_records": list(reversed(material_skip_records[-20:])),
        "progress_records": list(reversed(material_progress_records[-20:])),
        "records": material_outreach_timeline(
            material_send_records,
            material_skip_records,
            progress_records=material_progress_records,
            limit=20,
        ),
        "stats": material_outreach_stats(
            material_pool,
            material_send_records,
            material_skip_records,
            runtime_material_ids=runtime_material_ids,
        ),
        "browser": build_material_outreach_browser(dashboard_config.get('material_outreach_list', []), material_progress_records, material_pool),
    }
    material_outreach_runtime_seed = build_task_workbench_runtime_payload(
        "material_outreach",
        data_dir=DATA_DIR,
        wx_id=material_wx_id,
    )
    material_outreach_status["pending_count"] = (
        sum(
            1
            for item in material_outreach_runtime_seed.get("queue", [])
            if isinstance(item, dict) and str(item.get("status") or "").strip() == "pending"
        )
    )
    ai_candidate_count = len(build_ai_candidate_material_cards(material_pool))
    ai_material_outreach_status = {
        "pending_count": sum(1 for item in ai_pending_queue if str((item or {}).get('status') or '').strip() == 'pending'),
        "candidate_count": ai_candidate_count,
        "today_sent_count": sum(
            1
            for item in material_send_records
            if str((item or {}).get('task_id') or '').strip() == AI_AUTO_OUTREACH_TASK_ID
            and (item or {}).get('success')
            and (lambda sent_at: sent_at and sent_at.date() == datetime.now().date())(
                _material_outreach_parse_datetime((item or {}).get('sent_at'))
            )
        ),
    }
    contact_directory_options = _contact_profiles_picker_options(task_scope_wx_id)
    relationship_scan_seed = _relationship_scan_payload(contact_directory_options.get('wx_id', ''))
    friend_request_seed = _friend_request_payload(contact_directory_options.get('wx_id', ''))
    initial_active_tab = str(request.args.get('active_tab', '') or '').strip()

    force_admin_change_required = is_force_admin_change_required()
    return render_template(
        'dashboard.html',
        config=dashboard_config,
        keyword_rule_display_value=_keyword_rule_display_value,
        tts_sdk_options=list_tts_sdk_options(),
        tts_sdk_option_map={item['key']: item for item in list_tts_sdk_options()},
        tts_sdk_meta_map={item['key']: get_tts_sdk_meta(item['key']) for item in list_tts_sdk_options()},
        tts_model_options_by_sdk={item['key']: list_tts_model_options(item['key']) for item in list_tts_sdk_options()},
        logs=logger.get_recent_logs(limit=50),
        prompts=prompts,
        material_outreach_status=material_outreach_status,
        material_outreach_runtime_seed=material_outreach_runtime_seed,
        ai_material_outreach_status=ai_material_outreach_status,
        contact_directory_options=contact_directory_options,
        relationship_scan_seed=relationship_scan_seed,
        friend_request_seed=friend_request_seed,
        task_scope_options=task_scope_options,
        initial_active_tab=initial_active_tab,
        force_admin_change_required=force_admin_change_required,
        remote_connect_block_required=is_remote_connect_block_required(),
    )

@app.route('/get_logs')
@login_required
def get_logs():
    after_id_raw = str(request.args.get('after_id', '') or '').strip()
    after_id = None
    if after_id_raw:
        try:
            after_id = max(0, int(after_id_raw))
        except ValueError:
            after_id = None
    return jsonify(logger.get_logs_after(after_id, limit=50))


def _count_enabled_tasks(items):
    count = 0
    for item in items or []:
        if isinstance(item, dict):
            if item.get('enabled', True):
                count += 1
        elif item:
            count += 1
    return count


def _current_api_snapshot(cfg):
    api_configs = list((cfg or {}).get('api_configs') or [])
    if not api_configs:
        return {'sdk': '', 'model': '', 'current_interface': '未连接'}
    try:
        index = int((cfg or {}).get('api_index', 0) or 0)
    except Exception:
        index = 0
    index = max(0, min(index, len(api_configs) - 1))
    current = api_configs[index] if api_configs else {}
    return {
        'sdk': str(current.get('sdk', '') or '').strip(),
        'model': str(current.get('model', '') or '').strip(),
        'current_interface': format_api_display_name(api_configs, index, fallback='未连接'),
    }


def _dashboard_material_home_stats(wx_id='', runtime_material_ids=None):
    wx_id = str(wx_id or '').strip()
    runtime_ids = set(runtime_material_ids or [])
    if not wx_id:
        return {
            'available_materials': 0,
            'available_runtime_materials': len(runtime_ids),
            'today_touched': 0,
        }
    material_pool = _load_material_outreach_materials(wx_id)
    material_history = _load_material_outreach_history(wx_id)
    material_send_records = material_history['send_records']
    material_skip_records = material_history['skip_records']
    stats = material_outreach_stats(
        material_pool,
        material_send_records,
        material_skip_records,
        runtime_material_ids=runtime_ids,
    )
    return {
        'available_materials': int(stats.get('available_materials', 0) or 0),
        'available_runtime_materials': int(stats.get('available_runtime_materials', 0) or 0),
        'today_touched': int(((stats.get('today') or {}).get('success', 0)) or 0),
    }


def _dashboard_config_status_snapshot(cfg):
    cfg = cfg or {}
    api_snapshot = _current_api_snapshot(cfg)
    listen_list = [str(item).strip() for item in (cfg.get('listen_list', []) or []) if str(item).strip()]
    global_blacklist = [str(item).strip() for item in (cfg.get('global_blacklist', []) or []) if str(item).strip()]
    groups = [str(item).strip() for item in (cfg.get('group', []) or []) if str(item).strip()]
    material_task_count = _count_enabled_tasks(cfg.get('material_outreach_list', []))
    friend_request_state = friend_request.load_state(DATA_DIR, str(cfg.get('wx_id') or 'default'))
    friend_request_settings = friend_request.normalize_settings(friend_request_state.get('settings'))
    return {
        'version': str(BOT_VERSION or '').strip(),
        'wx_nickname': '',
        'api_name': api_snapshot.get('sdk', ''),
        'model': api_snapshot.get('model', ''),
        'current_interface': api_snapshot.get('current_interface', '未连接'),
        'listen_mode': '黑名单' if cfg.get('AllListen_switch') else '白名单',
        'listen_count': len(global_blacklist if cfg.get('AllListen_switch') else listen_list),
        'group_switch': bool(cfg.get('group_switch', False)),
        'group_count': len(groups),
        'msg_received': 0,
        'msg_replied': 0,
        'api_request_count': 0,
        'chat_api_requests': 0,
        'other_api_requests': 0,
        'last_msg_time': '',
        'last_msg_sender': '',
        'callback_is_die': False,
        'listener_recovery_active': False,
        'listener_recovery_status': 'idle',
        'listener_recovery_message': '',
        'listener_recovery_source': '',
        'listener_recovery_error': '',
        'scheduled_switch': _count_enabled_tasks(cfg.get('scheduled_message_task_list', [])) > 0,
        'scheduled_count': _count_enabled_tasks(cfg.get('scheduled_message_task_list', [])),
        'material_outreach_task_count': material_task_count,
        'chat_keyword_switch': bool(cfg.get('chat_keyword_switch', False)),
        'group_keyword_switch': bool(cfg.get('group_keyword_switch', False)),
        'group_keyword_at_only': bool(cfg.get('group_keyword_at_only', False)),
        'keyword_count': len(cfg.get('keyword_dict', {}) or {}),
        'memory_switch': bool(cfg.get('memory_switch', True)),
        'memory_context_switch': bool(cfg.get('memory_context_switch', cfg.get('memory_switch', True))),
        'memory_context_count': cfg.get('memory_context_count', 50),
        'memory_context_assistant_count': cfg.get('memory_context_assistant_count', 10),
        'reply_delay_switch': bool(cfg.get('reply_delay_switch', False)),
        'reply_delay_first_min': cfg.get('reply_delay_first_min', 0),
        'reply_delay_first_max': cfg.get('reply_delay_first_max', 0),
        'reply_delay_split_speed_mode': cfg.get('reply_delay_split_speed_mode', 'fast'),
        'reply_delay_split_min': cfg.get('reply_delay_split_min', 0),
        'reply_delay_split_max': cfg.get('reply_delay_split_max', 0),
        'default_prompt': str(cfg.get('default_prompt', '默认') or '默认').strip(),
        'clean_ai_reply_switch': bool(cfg.get('clean_ai_reply_switch', True)),
        'chat_voice_reply_switch': bool(cfg.get('chat_voice_reply_switch', False)),
        'group_voice_reply_switch': bool(cfg.get('group_voice_reply_switch', False)),
        'chat_image_recognition_switch': bool(cfg.get('chat_image_recognition_switch', False)),
        'group_image_recognition_switch': bool(cfg.get('group_image_recognition_switch', False)),
        'chat_split_reply_switch': bool(cfg.get('chat_split_reply_switch', False)),
        'group_split_reply_switch': bool(cfg.get('group_split_reply_switch', False)),
        'text_reply_limit_switch': bool(cfg.get('text_reply_limit_switch', False)),
        'text_reply_limit_count': cfg.get('text_reply_limit_count', 99),
        'text_reply_limit_hours': cfg.get('text_reply_limit_hours', 24),
        'new_friend_switch': bool(cfg.get('new_friend_switch', False)),
        'friend_request_enabled': bool(friend_request_settings.get('enabled', False)),
        'contact_directory_auto_maintenance_switch': bool(cfg.get('contact_directory_auto_maintenance_switch', False)),
        'start_time': '',
        'uptime': '',
    }


def _new_moments_task_id():
    return new_moments_task_id()


def _normalize_moments_task_images(value):
    return clean_moments_string_list(value, limit=9)


def _normalize_moments_task(task):
    return normalize_moments_task(task)


def _moments_tasks_wx_id_from_request():
    if request.method == 'GET':
        candidate = request.args.get('wx_id', '')
    elif request.is_json:
        payload = request.get_json(silent=True) or {}
        candidate = payload.get('wx_id', '')
    else:
        candidate = request.form.get('wx_id', '')
    wx_id = str(candidate or '').strip()
    if wx_id:
        return _validate_known_account_wx_id(wx_id)
    return _preferred_account_wx_id()


def _moments_tasks_file(wx_id, *, create_parent=False):
    if not wx_id:
        raise ValueError('wx_id is required')
    return str(account_module_file(DATA_DIR, wx_id, 'moments', 'tasks.json', create_parent=create_parent))


def _moments_runtime_file(wx_id, *, create_parent=False):
    if not wx_id:
        raise ValueError('wx_id is required')
    return str(account_module_file(DATA_DIR, wx_id, 'moments', 'runtime.json', create_parent=create_parent))


def _moments_history_file(wx_id, *, create_parent=False):
    if not wx_id:
        raise ValueError('wx_id is required')
    return str(account_module_file(DATA_DIR, wx_id, 'moments', 'history.json', create_parent=create_parent))


def _load_moments_tasks(wx_id=None):
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id()
    if not wx_id:
        return []
    definitions = load_json_list(_moments_tasks_file(wx_id))
    runtime_map = _load_json_object(_moments_runtime_file(wx_id))
    history_map = _load_json_object(_moments_history_file(wx_id))
    return deserialize_moments_task_collection(definitions, runtime_map, history_map)


def _save_moments_tasks(tasks, wx_id=None):
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id()
    if not wx_id:
        return False
    normalized = [_normalize_moments_task(task) for task in (tasks or []) if isinstance(task, dict)]
    definitions, runtime_map, history_map = serialize_moments_task_collection(normalized)
    save_json_list(_moments_tasks_file(wx_id, create_parent=True), definitions)
    _save_json_object(_moments_runtime_file(wx_id, create_parent=True), runtime_map)
    _save_json_object(_moments_history_file(wx_id, create_parent=True), history_map)
    return True


def _delete_managed_moments_task_uploads(task, wx_id=None):
    task = task if isinstance(task, dict) else {}
    if str(task.get('file_storage_mode') or '').strip() != 'managed':
        return 0
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id()
    if not wx_id:
        return 0
    return delete_managed_moments_uploads(
        task.get('images') or [],
        data_dir=DATA_DIR,
        wx_id=wx_id,
    )


def _moments_task_counts(tasks):
    return moments_task_counts(tasks)


def _parse_moments_task_items_from_request():
    if not request.is_json:
        return []
    payload = request.get_json(silent=True) or {}
    items = payload.get('items') or []
    return items if isinstance(items, list) else []


def _moments_tasks_payload(tasks):
    titled_tasks = _apply_moments_display_titles(tasks)
    return {
        'status': 'success',
        'tasks': titled_tasks,
        'counts': _moments_task_counts(titled_tasks),
    }


def _request_moments_runtime_reload():
    runtime_bot = globals().get('bot')
    if runtime_bot and hasattr(runtime_bot, 'request_runtime_task_reload'):
        try:
            runtime_bot.request_runtime_task_reload()
        except Exception as exc:
            log('WARNING', f'运行中朋友圈任务同步失败，将在下次刷新后生效：{exc}')


def _parse_moments_datetime(value):
    text = str(value or '').strip()
    if not text:
        return None
    if len(text) == 16 and 'T' in text:
        text = f'{text}:00'
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _parse_moments_hhmm(value, default):
    text = str(value or default).strip() or default
    match = re.search(r'(\d{1,2}):(\d{1,2})', text)
    if not match:
        text = default
        match = re.search(r'(\d{1,2}):(\d{1,2})', text)
    hour = max(0, min(23, int(match.group(1))))
    minute = max(0, min(59, int(match.group(2))))
    return hour, minute


def _resolve_moments_execute_after(task, *, mode='queue', now=None):
    return resolve_moments_execute_after(task, mode=mode, now=now)


def _find_moments_task(tasks, task_id):
    for task in tasks or []:
        if task.get('id') == task_id:
            return task
    return None


def _resolve_moments_api_index(cfg):
    cfg = cfg if isinstance(cfg, dict) else {}
    api_configs = cfg.get('api_configs') if isinstance(cfg.get('api_configs'), list) else []
    if not api_configs:
        return -1
    try:
        configured = int(cfg.get('moments_api_index', 0))
    except (TypeError, ValueError):
        configured = 0
    if 0 <= configured < len(api_configs):
        return configured
    return 0


def _api_config_supports_vision(cfg, index):
    cfg = cfg if isinstance(cfg, dict) else {}
    return api_supports_capability(cfg.get('api_capability_map'), index, 'vision')


def _extract_moments_response_text(data):
    if not isinstance(data, dict):
        return ""
    output_text = data.get('output_text')
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    result_parts = []
    for item in data.get('output', []) if isinstance(data.get('output'), list) else []:
        if not isinstance(item, dict):
            continue
        for block in item.get('content', []) if isinstance(item.get('content'), list) else []:
            if not isinstance(block, dict):
                continue
            if block.get('type') in ('output_text', 'text') and block.get('text'):
                result_parts.append(str(block.get('text')))
    return ''.join(result_parts).strip()


def _parse_moments_candidates(raw_reply):
    return parse_moments_candidates(raw_reply, cleaner=sanitize_ai_output_text)


def _generate_moments_candidates_for_task(task, cfg=None, wx_id=""):
    task = task if isinstance(task, dict) else {}
    cfg = cfg if isinstance(cfg, dict) else (read_config() or {})
    if not task.get('raw_text') and not task.get('images'):
        raise ValueError('请先添加文案或图片')
    api_configs = cfg.get('api_configs') if isinstance(cfg.get('api_configs'), list) else []
    api_index = _resolve_moments_api_index(cfg)
    if api_index < 0 or api_index >= len(api_configs):
        raise ValueError('请先配置朋友圈 AI 接口')
    api_config = api_configs[api_index]
    image_paths = []
    for image in task.get('images') or []:
        image_path = str(image or '').strip()
        if os.path.isfile(image_path):
            image_paths.append(image_path)
    if image_paths and not _api_config_supports_vision(cfg, api_index):
        raise ValueError('当前朋友圈接口未启用图片识别，请先测试接口并确认该接口支持识图')
    api = _build_test_api_client(_build_panel_generation_api_config(api_config))
    prompt = _build_moments_generation_prompt(task, cfg=cfg, wx_id=wx_id)
    raw_text = str(task.get('raw_text') or '').strip()
    message_text = '请基于这次素材生成 3 条可直接发布的朋友圈文案候选。'
    if raw_text:
        message_text = f'{message_text}\n\n原始短文案：\n{raw_text}'
    raw_reply = _call_moments_multi_image_api(
        api=api,
        prompt=prompt,
        message_text=message_text,
        image_paths=image_paths,
    )
    return _parse_moments_candidates(raw_reply)


def _build_moments_generation_prompt(task, cfg=None, wx_id=""):
    cfg = cfg if isinstance(cfg, dict) else (read_config() or {})
    raw_text = str((task or {}).get('raw_text') or '').strip() or '（未提供）'
    wx_id = str(wx_id or '').strip() or _preferred_account_wx_id(CHAT_MEMORY_BASE)
    state_dir = _account_chat_memory_dir(wx_id, create=True)
    system = PromptSystem(cfg, state_dir=state_dir, prompt_dir=PROMPT_DIR)
    prompt_source_name = str(cfg.get('cmd') or cfg.get('default_prompt') or '').strip()
    return system.render_moments_caption_prompt(prompt_source_name, raw_text, chat_type='private')


def _call_moments_multi_image_api(*, api, prompt, message_text, image_paths):
    kwargs = {
        'prompt': prompt,
        'history': [],
        'stream': False,
    }
    if len(image_paths) > 1:
        kwargs['image_paths'] = image_paths
    elif image_paths:
        kwargs['image_path'] = image_paths[0]
    return api.chat(message_text, **kwargs)


@app.route('/api/moments/tasks', methods=['GET'])
@login_required
def api_moments_tasks_list():
    wx_id = _moments_tasks_wx_id_from_request()
    tasks = _load_moments_tasks(wx_id)
    return jsonify(_moments_tasks_payload(tasks))


@app.route('/api/local-image-preview', methods=['GET'])
@login_required
def api_local_image_preview():
    raw_path = str(request.args.get('path', '') or '').strip()
    allowed_exts = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'}
    if not raw_path:
        return jsonify({'status': 'error', 'message': '缺少图片路径'}), 400
    image_path = os.path.abspath(os.path.normpath(raw_path))
    if os.path.splitext(image_path)[1].lower() not in allowed_exts:
        return jsonify({'status': 'error', 'message': '仅支持预览图片文件'}), 400
    if not os.path.isfile(image_path):
        return jsonify({'status': 'error', 'message': '图片不存在'}), 404
    return send_file(image_path)


@app.route('/api/moments/tasks', methods=['POST'])
@login_required
def api_moments_tasks_create():
    items = _parse_moments_task_items_from_request()
    if not items:
        return jsonify({'status': 'error', 'message': '请至少添加一组朋友圈素材'}), 400
    wx_id = _moments_tasks_wx_id_from_request()
    if not wx_id:
        return jsonify({'status': 'error', 'message': '请先选择微信号后再创建朋友圈任务'}), 400

    existing_tasks = _load_moments_tasks(wx_id)
    created_tasks = []
    warnings = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        task_id = _new_moments_task_id()
        image_files = _normalize_moments_task_images(item.get('images'))
        file_storage_mode = str(item.get('file_storage_mode') or '').strip()
        if file_storage_mode not in ('direct', 'managed'):
            file_storage_mode = 'direct'
        raw_task = {
            **item,
            'id': task_id,
            'source': item.get('source') or 'web_panel',
            'file_storage_mode': file_storage_mode,
            'raw_text': str(item.get('raw_text') or '').strip(),
            'images': image_files[:9],
            'status': 'pending_confirm',
            'copy_mode': item.get('copy_mode') or 'ai',
            'publish_rule': item.get('publish_rule') or 'random',
            'publish_time': item.get('publish_time') or default_moments_publish_time(),
            'publish_window': item.get('publish_window') or latest_moments_random_window(existing_tasks + created_tasks),
        }
        task = _normalize_moments_task(raw_task)
        if not task['raw_text'] and not task['images']:
            continue
        task = _normalize_moments_task({
            **task,
            'copy_mode': 'ai',
            'candidates': [],
            'selected_caption': task.get('raw_text') or '无文案',
            'ai_generation_status': 'pending',
            'ai_generation_error': '',
        })
        created_tasks.append(task)

    if not created_tasks:
        return jsonify({'status': 'error', 'message': '请至少填写一段文案，或添加一张图片'}), 400

    tasks = created_tasks + existing_tasks
    if not _save_moments_tasks(tasks, wx_id):
        return jsonify({'status': 'error', 'message': '保存朋友圈任务失败'}), 500
    _request_moments_runtime_reload()
    return jsonify({
        **_moments_tasks_payload(tasks),
        'created': created_tasks,
        'warnings': warnings,
    })


@app.route('/api/moments/tasks/<task_id>', methods=['DELETE'])
@login_required
def api_moments_tasks_delete(task_id):
    task_id = str(task_id or '').strip()
    wx_id = _moments_tasks_wx_id_from_request()
    tasks = _load_moments_tasks(wx_id)
    removed_tasks = [task for task in tasks if task.get('id') == task_id]
    next_tasks = [task for task in tasks if task.get('id') != task_id]
    if len(next_tasks) == len(tasks):
        return jsonify({'status': 'error', 'message': '朋友圈任务不存在'}), 404
    if not _save_moments_tasks(next_tasks, wx_id):
        return jsonify({'status': 'error', 'message': '删除朋友圈任务失败'}), 500
    for task in removed_tasks:
        _delete_managed_moments_task_uploads(task, wx_id)
    _request_moments_runtime_reload()
    return jsonify(_moments_tasks_payload(next_tasks))


@app.route('/api/moments/tasks/<task_id>', methods=['PATCH'])
@login_required
def api_moments_tasks_update(task_id):
    task_id = str(task_id or '').strip()
    payload = request.get_json(silent=True) or {}
    wx_id = _moments_tasks_wx_id_from_request()
    tasks = _load_moments_tasks(wx_id)
    updated_task = None
    next_tasks = []
    for task in tasks:
        if task.get('id') != task_id:
            next_tasks.append(task)
            continue
        if task.get('status') == 'pending':
            return jsonify({'status': 'error', 'message': '这条朋友圈任务正在等待发布，请先取消后再编辑'}), 400
        allowed_updates = {}
        for key in (
            'enabled',
            'copy_mode',
            'raw_text',
            'images',
            'candidates',
            'selected_caption',
            'publish_rule',
            'publish_time',
            'publish_window',
            'visibility_type',
            'tags',
        ):
            if key in payload:
                allowed_updates[key] = payload[key]
        allowed_updates['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        updated_task = _normalize_moments_task({**task, **allowed_updates})
        next_tasks.append(updated_task)
    if updated_task is None:
        return jsonify({'status': 'error', 'message': '朋友圈任务不存在'}), 404
    if not _save_moments_tasks(next_tasks, wx_id):
        return jsonify({'status': 'error', 'message': '保存朋友圈任务失败'}), 500
    _request_moments_runtime_reload()
    return jsonify({
        **_moments_tasks_payload(next_tasks),
        'task': updated_task,
    })


def _task_workbench_wx_id_from_request(module):
    module = str(module or '').strip()
    if module == 'scheduled_message':
        return _scheduled_message_wx_id_from_request()
    if module == 'moments':
        return _moments_tasks_wx_id_from_request()
    if module == 'material_outreach':
        return _current_material_outreach_wx_id()
    return ''


def _save_task_switch_minimal(switch_key, value):
    switch_key = str(switch_key or '').strip()
    if not switch_key:
        return True
    cfg = read_config() or {}
    desired = bool(value)
    if bool(cfg.get(switch_key, False)) is desired:
        return True
    cfg[switch_key] = desired
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=4)
        return True
    except Exception as e:
        log('ERROR', f'保存任务开关失败: {e}')
        return False


def _task_workbench_hooks(module, wx_id):
    module = str(module or '').strip()
    wx_id = str(wx_id or '').strip()
    hooks = {}
    if module == 'scheduled_message':
        hooks['reload_runtime'] = _request_scheduled_message_runtime_reload
    elif module == 'moments':
        hooks['reload_runtime'] = _request_moments_runtime_reload
    elif module == 'material_outreach':
        hooks['reload_runtime'] = _request_material_runtime_reload
    return hooks


@app.route('/api/task-workbench/<module>', methods=['GET'])
@login_required
def api_task_workbench_payload(module):
    try:
        wx_id = _task_workbench_wx_id_from_request(module)
        payload = build_task_workbench_payload(
            module,
            data_dir=DATA_DIR,
            wx_id=wx_id,
            active_task_id=request.args.get('active_task_id', ''),
        )
        if module == 'material_outreach':
            config = _inject_account_scoped_task_config(read_config() or {}, wx_id=wx_id)
            materials = _load_material_outreach_materials(wx_id)
            history = _load_material_outreach_history(wx_id)
            runtime_ids = _current_material_runtime_ids()
            payload['materials'] = build_material_management_view(materials, runtime_ids)
            payload['stats'] = material_outreach_stats(
                materials,
                history['send_records'],
                history['skip_records'],
                runtime_material_ids=runtime_ids,
            )
            payload['browser'] = build_material_outreach_browser(
                config.get('material_outreach_list', []),
                history['progress_records'],
                materials,
            )
            runtime_queue = payload.get('runtime', {}).get('queue', [])
            payload['queue'] = runtime_queue
            payload['pending_count'] = sum(
                1 for item in runtime_queue
                if isinstance(item, dict) and str(item.get('status') or '').strip() == 'pending'
            )
        return jsonify(payload)
    except TaskWorkbenchServiceError as exc:
        return jsonify({'status': 'error', 'message': exc.message}), exc.status_code
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/task-workbench/<module>/runtime', methods=['GET'])
@login_required
def api_task_workbench_runtime(module):
    try:
        wx_id = _task_workbench_wx_id_from_request(module)
        runtime_payload = build_task_workbench_runtime_payload(
            module,
            data_dir=DATA_DIR,
            wx_id=wx_id,
        )
        return jsonify(runtime_payload)
    except TaskWorkbenchServiceError as exc:
        return jsonify({'status': 'error', 'message': exc.message}), exc.status_code
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/task-workbench/<module>/tasks/<task_id>/queue', methods=['POST'])
@login_required
def api_task_workbench_queue_task(module, task_id):
    try:
        payload = request.get_json(silent=True) if request.is_json else {}
        payload = payload if isinstance(payload, dict) else {}
        wx_id = _task_workbench_wx_id_from_request(module)
        result = queue_task_in_workbench(
            module,
            task_id,
            data_dir=DATA_DIR,
            wx_id=wx_id,
            payload=payload,
            hooks=_task_workbench_hooks(module, wx_id),
        )
        return jsonify(result)
    except TaskWorkbenchServiceError as exc:
        return jsonify({'status': 'error', 'message': exc.message}), exc.status_code
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/task-workbench/<module>/queue/<path:queue_id>', methods=['DELETE'])
@login_required
def api_task_workbench_cancel_queue_item(module, queue_id):
    try:
        wx_id = _task_workbench_wx_id_from_request(module)
        result = cancel_task_workbench_queue_item(
            module,
            queue_id,
            data_dir=DATA_DIR,
            wx_id=wx_id,
            hooks=_task_workbench_hooks(module, wx_id),
        )
        return jsonify(result)
    except TaskWorkbenchServiceError as exc:
        return jsonify({'status': 'error', 'message': exc.message}), exc.status_code
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/task-workbench/<module>/executions', methods=['DELETE'])
@login_required
def api_task_workbench_clear_executions(module):
    try:
        wx_id = _task_workbench_wx_id_from_request(module)
        result = clear_task_workbench_executions(
            module,
            data_dir=DATA_DIR,
            wx_id=wx_id,
            hooks=_task_workbench_hooks(module, wx_id),
        )
        return jsonify(result)
    except TaskWorkbenchServiceError as exc:
        return jsonify({'status': 'error', 'message': exc.message}), exc.status_code
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/moments/settings', methods=['PATCH'])
@login_required
def api_moments_settings_update():
    payload = request.get_json(silent=True) or {}
    cfg = read_config() or {}
    api_configs = cfg.get('api_configs') if isinstance(cfg.get('api_configs'), list) else []
    try:
        api_index = int(payload.get('moments_api_index', cfg.get('moments_api_index', 0)))
    except (TypeError, ValueError):
        api_index = 0
    if api_index < 0 or api_index >= len(api_configs):
        return jsonify({'status': 'error', 'message': '朋友圈接口不存在'}), 400
    if not save_config({'moments_api_index': api_index}):
        return jsonify({'status': 'error', 'message': '保存朋友圈接口失败'}), 500
    _request_moments_runtime_reload()
    merged = read_config() or {}
    return jsonify({
        'status': 'success',
        'moments_api_index': int(merged.get('moments_api_index', 0) or 0),
        'resolved_api_index': _resolve_moments_api_index(merged),
    })


@app.route('/api/moments/tasks/<task_id>/generate', methods=['POST'])
@login_required
def api_moments_tasks_generate(task_id):
    task_id = str(task_id or '').strip()
    cfg = read_config() or {}
    wx_id = _moments_tasks_wx_id_from_request()
    tasks = _load_moments_tasks(wx_id)
    task = _find_moments_task(tasks, task_id)
    if task is None:
        return jsonify({'status': 'error', 'message': '朋友圈任务不存在'}), 404
    if task.get('status') == 'pending':
        return jsonify({'status': 'error', 'message': '这条朋友圈任务正在等待发布，请先取消后再重新生成'}), 400
    pending_task = _normalize_moments_task({
        **task,
        'enabled': True,
        'status': 'pending_confirm',
        'copy_mode': 'ai',
        'candidates': [],
        'selected_caption': task.get('raw_text') or '无文案',
        'execute_after': '',
        'queued_at': '',
        'queued_mode': '',
        'executed_at': '',
        'execution_result': '',
        'execution_message': '',
        'ai_generation_status': 'pending',
        'ai_generation_error': '',
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    })
    pending_tasks = [pending_task if item.get('id') == task_id else item for item in tasks]
    if not _save_moments_tasks(pending_tasks, wx_id):
        return jsonify({'status': 'error', 'message': '保存朋友圈任务失败'}), 500
    _request_moments_runtime_reload()

    def _generation_failed_payload(message, *, status_code):
        failed_task = _normalize_moments_task({
            **pending_task,
            'status': 'pending_confirm',
            'ai_generation_status': 'failed',
            'ai_generation_error': message,
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        })
        latest_tasks = _load_moments_tasks(wx_id)
        latest_task = _find_moments_task(latest_tasks, task_id) or pending_task
        if latest_task:
            latest_task = _normalize_moments_task({
                **latest_task,
                'status': 'pending_confirm',
                'ai_generation_status': 'failed',
                'ai_generation_error': message,
                'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            })
            latest_tasks = [latest_task if item.get('id') == task_id else item for item in latest_tasks]
        else:
            latest_tasks = [failed_task if item.get('id') == task_id else item for item in pending_tasks]
        _save_moments_tasks(latest_tasks, wx_id)
        reloaded_tasks = _load_moments_tasks(wx_id)
        latest_task = _find_moments_task(reloaded_tasks, task_id) or failed_task
        return jsonify({
            'status': 'error',
            'message': message,
            **_moments_tasks_payload(reloaded_tasks or latest_tasks),
            'task': latest_task,
        }), status_code

    try:
        candidates = _generate_moments_candidates_for_task(pending_task, cfg=cfg, wx_id=wx_id)
    except ValueError as exc:
        return _generation_failed_payload(str(exc), status_code=400)
    except Exception as exc:
        log('ERROR', f'朋友圈文案生成失败：{exc}')
        error_message = str(exc).strip()
        if not error_message:
            error_message = '请检查接口状态'
        return _generation_failed_payload(f'生成失败：{error_message}', status_code=500)

    latest_tasks = _load_moments_tasks(wx_id)
    latest_task = _find_moments_task(latest_tasks, task_id)
    if latest_task is None:
        return jsonify({
            'status': 'error',
            'message': '朋友圈任务不存在',
            **_moments_tasks_payload(latest_tasks),
        }), 404
    updated_task = _normalize_moments_task({
        **latest_task,
        'enabled': True,
        'status': 'pending_confirm',
        'candidates': candidates,
        'selected_caption': candidates[0],
        'execute_after': '',
        'queued_at': '',
        'queued_mode': '',
        'executed_at': '',
        'execution_result': '',
        'execution_message': '',
        'ai_generation_status': 'done',
        'ai_generation_error': '',
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    })
    next_tasks = [updated_task if item.get('id') == task_id else item for item in latest_tasks]
    if not _save_moments_tasks(next_tasks, wx_id):
        return jsonify({'status': 'error', 'message': '保存朋友圈候选失败'}), 500
    _request_moments_runtime_reload()
    return jsonify({
        **_moments_tasks_payload(next_tasks),
        'task': updated_task,
    })


def _runtime_metrics_today(payload):
    try:
        today = (payload or {}).get('today') or {}
        return today if isinstance(today, dict) else {}
    except Exception:
        return {}


def _runtime_metric_count(today, key):
    try:
        value = int((today or {}).get(key, 0) or 0)
        return value if value >= 0 else 0
    except Exception:
        return 0


def _dashboard_runtime_metrics_payload(days=1, runtime_bot=None):
    try:
        days = int(days or 1)
    except (TypeError, ValueError):
        days = 1
    days = max(1, min(365, days))
    if runtime_bot is not None and hasattr(runtime_bot, 'get_runtime_metrics_series'):
        try:
            payload = runtime_bot.get_runtime_metrics_series(days=days)
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
    try:
        return RuntimeMetricsStore(os.path.join(CONFIG_DIR, 'runtime_metrics_v1.json')).series_payload(days=days)
    except Exception:
        return None


def _sync_status_api_count_from_runtime_metrics(status, payload=None, *, runtime_bot=None):
    status = dict(status or {})
    if payload is None:
        payload = _dashboard_runtime_metrics_payload(days=1, runtime_bot=runtime_bot)
    today = _runtime_metrics_today(payload)
    if today:
        api_calls = _runtime_metric_count(today, 'api_calls')
        chat_api_calls = _runtime_metric_count(today, 'chat_api_calls')
        status['msg_received'] = _runtime_metric_count(today, 'received_messages')
        status['msg_replied'] = _runtime_metric_count(today, 'reply_count')
        status['api_request_count'] = api_calls
        status['chat_api_requests'] = chat_api_calls
        status['other_api_requests'] = max(0, api_calls - chat_api_calls)
    return status


def _enrich_dashboard_status_snapshot(status, *, cfg=None, wx_id='', runtime_material_ids=None, runtime_metrics_payload=None):
    cfg = cfg or {}
    status = dict(status or {})
    status = _sync_status_api_count_from_runtime_metrics(status, runtime_metrics_payload)
    listen_list = [str(item).strip() for item in (cfg.get('listen_list', []) or []) if str(item).strip()]
    global_blacklist = [str(item).strip() for item in (cfg.get('global_blacklist', []) or []) if str(item).strip()]
    groups = [str(item).strip() for item in (cfg.get('group', []) or []) if str(item).strip()]
    status['listen_mode'] = '黑名单' if cfg.get('AllListen_switch') else '白名单'
    status['listen_count'] = len(global_blacklist if cfg.get('AllListen_switch') else listen_list)
    status['group_switch'] = bool(cfg.get('group_switch', False))
    status['group_count'] = len(groups)
    if 'current_interface' not in status:
        status['current_interface'] = _current_api_snapshot(cfg).get('current_interface', '未连接')
    scheduled_count = _count_enabled_tasks(cfg.get('scheduled_message_task_list', []))
    if 'scheduled_count' not in status:
        status['scheduled_count'] = scheduled_count
    if 'scheduled_switch' not in status:
        status['scheduled_switch'] = scheduled_count > 0
    if 'material_outreach_task_count' not in status:
        status['material_outreach_task_count'] = _count_enabled_tasks(cfg.get('material_outreach_list', []))
    if 'new_friend_switch' not in status:
        status['new_friend_switch'] = bool(cfg.get('new_friend_switch', False))
    if 'contact_directory_auto_maintenance_switch' not in status:
        status['contact_directory_auto_maintenance_switch'] = bool(cfg.get('contact_directory_auto_maintenance_switch', False))
    status['chat_image_recognition_switch'] = bool(status.get('chat_image_recognition_switch', cfg.get('chat_image_recognition_switch', False)))
    status['group_image_recognition_switch'] = bool(status.get('group_image_recognition_switch', cfg.get('group_image_recognition_switch', False)))
    try:
        friend_request_state = friend_request.load_state(DATA_DIR, str(wx_id or cfg.get('wx_id') or 'default'))
        friend_request_settings = friend_request.normalize_settings(friend_request_state.get('settings'))
        status['friend_request_enabled'] = bool(friend_request_settings.get('enabled', False))
    except Exception:
        status['friend_request_enabled'] = False
    status['default_prompt'] = str(cfg.get('default_prompt', '默认') or '默认').strip()
    status['clean_ai_reply_switch'] = bool(cfg.get('clean_ai_reply_switch', True))
    status['chat_voice_reply_switch'] = bool(cfg.get('chat_voice_reply_switch', False))
    status['group_voice_reply_switch'] = bool(cfg.get('group_voice_reply_switch', False))
    status['chat_split_reply_switch'] = bool(cfg.get('chat_split_reply_switch', False))
    status['group_split_reply_switch'] = bool(cfg.get('group_split_reply_switch', False))
    material_stats = _dashboard_material_home_stats(wx_id=wx_id, runtime_material_ids=runtime_material_ids)
    status['material_today_touched'] = material_stats['today_touched']
    return status

def _coerce_bool_fields(merged_config):
    boolean_fields = [
        'AllListen_switch',
        'AllListen_filter_mute',
        'chat_listen_only',
        'chat_voice_recognition_switch',
        'voice_transcription_fallback_reply_once',
        'group_switch',
        'group_listen_only',
        'group_reply_at',
        'group_reply_at_msg',
        'group_reply_quote',
        'group_welcome',
        'new_friend_switch',
        'new_friend_archive_switch',
        'new_friend_reply_switch',
        'new_friend_remark_use_nickname',
        'new_friend_remark_prefix_timestamp',
        'new_friend_remark_suffix_timestamp',
        # —— 新增布尔字段 ——
        'chat_keyword_switch',
        'group_keyword_switch',
        'group_keyword_at_only',
        'ai_material_outreach_switch',
        'contact_directory_auto_maintenance_switch',
        'material_source_silent',           # 素材源非素材消息静默
        'moments_like_switch',              # 随机朋友圈点赞开关
        'everyday_start_stop_bot_switch',   # 新增
        'memory_switch',                    # 聊天记录保存开关
        'memory_context_switch',            # 最近聊天带入开关
        'reply_delay_switch',               # 发送延迟开关
        'clean_ai_reply_switch',            # AI 回复清洗开关
        'chat_image_recognition_switch',    # 私聊图片识别开关
        'group_image_recognition_switch',   # 群组图片识别开关
        'group_voice_recognition_switch',   # 群组语音转文字开关
        'custom_forward_switch',            # 自定义转发总开关
        'chat_split_reply_switch',          # 私聊拆分多条回复开关
        'group_split_reply_switch',         # 群聊拆分多条回复开关
        'chat_voice_reply_switch',
        'group_voice_reply_switch',
        'siver_panel_enabled',
        'api_error_reply_once',             # API错误只回复一次
        'meta_reply_blocked_reply_once',    # 元话术固定回复只回复一次
        'text_reply_limit_switch',          # 单用户文本回复次数限制开关
        'text_reply_limit_ai_reply',        # 超限后AI自动生成结束语
        'text_reply_limit_reply_once',      # 超限后只回复一次
        'chat_memory_switch',
        'ai_material_outreach_preface_enabled',
    ]
    for field in boolean_fields:
        if field in merged_config:
            v = merged_config[field]
            if isinstance(v, str):
                merged_config[field] = (v.lower() in ('on', 'true', '1'))
            else:
                merged_config[field] = bool(v)

def _coerce_list_fields(merged_config):
    list_fields = ['listen_list', 'global_blacklist', 'group', 'new_friend_tags', 'scheduled_message_task_list', 'material_source_list', 'material_outreach_list', 'moments_task_list', 'custom_forward_list', 'chat_memory_exclude_list', 'chat_voice_reply_request_keywords', 'group_voice_reply_request_keywords', 'chat_voice_reply_trigger_modes', 'ai_material_outreach_allowed_sources']
    for field in list_fields:
        if field in merged_config and not isinstance(merged_config[field], list):
            if isinstance(merged_config[field], str):
                merged_config[field] = [merged_config[field]] if merged_config[field] else []
            else:
                merged_config[field] = []
        if field in merged_config:
            merged_config[field] = [item for item in merged_config[field] if str(item).strip()]

def _coerce_float_fields(merged_config):
    if 'chat_message_merge_delay' in merged_config:
        merged_config['chat_message_merge_delay'] = coerce_float_range(
            merged_config['chat_message_merge_delay'], 3.0, 0.0, 10.0
        )
    # 仅当前需要 group_welcome_random，限定 [0.0, 1.0]
    if 'group_welcome_random' in merged_config:
        try:
            val = float(merged_config['group_welcome_random'])
            if val < 0.0: val = 0.0
            if val > 1.0: val = 1.0
            merged_config['group_welcome_random'] = val
        except (TypeError, ValueError):
            # 若非法，则保持原值或回退默认
            merged_config['group_welcome_random'] = float(read_config().get('group_welcome_random', 1.0))


def _clean_unique_string_list(value):
    if isinstance(value, list):
        raw_values = value
    elif value is None:
        raw_values = []
    else:
        raw_values = [value]
    result = []
    seen = set()
    for item in raw_values:
        text = str(item or '').strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _split_inline_keyword_list(value):
    if isinstance(value, str):
        tokens = re.split(r"[；;\n\r]+", value)
        return _clean_unique_string_list(tokens)
    return _clean_unique_string_list(value)


def _normalize_material_target_selector(value):
    selector = value if isinstance(value, dict) else {}
    include_tags = _clean_unique_string_list(selector.get('include_tags'))
    exclude_tags = _clean_unique_string_list(selector.get('exclude_tags'))
    mode = str(selector.get('mode') or '').strip()
    if mode not in {'all', 'include', 'exclude'}:
        if include_tags or str(selector.get('base') or '').strip() == 'manual':
            mode = 'include'
        elif exclude_tags:
            mode = 'exclude'
        else:
            mode = 'all'
    return {
        'mode': mode,
        'base': 'manual' if mode == 'include' and not include_tags else 'all_friends',
        'include_tags': include_tags if mode == 'include' else [],
        'exclude_tags': exclude_tags if mode == 'exclude' else [],
        'include_contact_keys': [],
        'exclude_contact_keys': [],
    }


def _coerce_int_range_fields(merged_config):
    """对整数范围字段做类型校验和区间限制"""
    int_range_fields = {
        'new_friend_check_min': (60, 3600, 60),
        'new_friend_check_max': (60, 3600, 300),
        'memory_max_count': (100, 5000, 5000),
        'memory_context_count': (1, 100, 50),
        'memory_context_assistant_count': (0, 100, 10),
        'text_reply_limit_count': (0, 99999, 99),
        'text_reply_limit_hours': (0, 720, 24),
        'chat_memory_message_threshold': (10, 200, 100),
        'chat_memory_interval_hours': (1, 72, 12),
        'chat_memory_protected_recent_count': (0, 200, 20),
        'contact_directory_auto_maintenance_batch_size': (20, 80, 50),
        'contact_directory_auto_maintenance_interval_minutes': (5, 1440, 20),
        'contact_directory_auto_maintenance_full_scan_interval_days': (1, 30, 7),
        'backup_chat_api_failover_threshold': (1, 10, 3),
        'chat_voice_reply_cooldown_minutes': (0, 1440, 10),
        'chat_voice_reply_limit_count': (0, 99, 50),
        'chat_voice_reply_limit_hours': (0, 720, 24),
        'chat_voice_session_minutes': (1, 1440, 10),
        'chat_voice_session_turns': (1, 20, 5),
        'group_voice_reply_cooldown_minutes': (0, 1440, 0),
        'group_voice_reply_limit_count': (0, 99, 99),
        'group_voice_reply_limit_hours': (0, 720, 24),
    }
    for field, (lo, hi, default) in int_range_fields.items():
        if field in merged_config:
            try:
                val = int(merged_config[field])
                merged_config[field] = max(lo, min(hi, val))
            except (TypeError, ValueError):
                merged_config[field] = default
    # 保证 min <= max
    if 'new_friend_check_min' in merged_config and 'new_friend_check_max' in merged_config:
        if merged_config['new_friend_check_min'] > merged_config['new_friend_check_max']:
            merged_config['new_friend_check_max'] = merged_config['new_friend_check_min']
    if 'memory_max_count' in merged_config and 'memory_context_count' in merged_config:
        if merged_config['memory_context_count'] > merged_config['memory_max_count']:
            merged_config['memory_context_count'] = merged_config['memory_max_count']
    if 'memory_context_count' in merged_config and 'memory_context_assistant_count' in merged_config:
        if merged_config['memory_context_assistant_count'] > merged_config['memory_context_count']:
            merged_config['memory_context_assistant_count'] = merged_config['memory_context_count']

    reply_delay_first_min = merged_config.get('reply_delay_first_min', 1)
    reply_delay_first_max = merged_config.get('reply_delay_first_max', 5)
    reply_delay_split_min = merged_config.get('reply_delay_split_min', reply_delay_first_min)
    reply_delay_split_max = merged_config.get('reply_delay_split_max', reply_delay_first_max)
    split_speed_mode = str(merged_config.get('reply_delay_split_speed_mode', 'fast') or 'fast').strip().lower()
    if split_speed_mode not in ('fast', 'normal', 'slow'):
        split_speed_mode = 'fast'
    merged_config['reply_delay_split_speed_mode'] = split_speed_mode
    try:
        merged_config['reply_delay_first_min'] = max(1, min(600, int(reply_delay_first_min)))
    except (TypeError, ValueError):
        merged_config['reply_delay_first_min'] = 1
    try:
        merged_config['reply_delay_first_max'] = max(1, min(600, int(reply_delay_first_max)))
    except (TypeError, ValueError):
        merged_config['reply_delay_first_max'] = 5
    try:
        merged_config['reply_delay_split_min'] = max(1, min(600, int(reply_delay_split_min)))
    except (TypeError, ValueError):
        merged_config['reply_delay_split_min'] = merged_config['reply_delay_first_min']
    try:
        merged_config['reply_delay_split_max'] = max(1, min(600, int(reply_delay_split_max)))
    except (TypeError, ValueError):
        merged_config['reply_delay_split_max'] = merged_config['reply_delay_first_max']
    if isinstance(merged_config.get('material_outreach_list'), list):
        merged_config['material_outreach_list'] = [
            normalize_material_outreach_task(task)
            for task in merged_config['material_outreach_list']
            if isinstance(task, dict)
        ]
    if 'contact_directory_auto_maintenance_batch_size' in merged_config:
        merged_config['contact_directory_auto_maintenance_batch_size'] = normalize_auto_maintenance_batch_size(
            merged_config.get('contact_directory_auto_maintenance_batch_size')
        )
    if 'contact_directory_auto_maintenance_interval_minutes' in merged_config:
        merged_config['contact_directory_auto_maintenance_interval_minutes'] = coerce_auto_maintenance_interval_minutes(
            merged_config.get('contact_directory_auto_maintenance_interval_minutes')
        )
    if 'contact_directory_auto_maintenance_full_scan_interval_days' in merged_config:
        merged_config['contact_directory_auto_maintenance_full_scan_interval_days'] = coerce_auto_maintenance_full_scan_interval_days(
            merged_config.get('contact_directory_auto_maintenance_full_scan_interval_days')
        )
    if 'contact_directory_auto_maintenance_window_start' in merged_config:
        merged_config['contact_directory_auto_maintenance_window_start'] = coerce_auto_maintenance_window_time(
            merged_config.get('contact_directory_auto_maintenance_window_start'),
            '00:00',
        )
    if 'contact_directory_auto_maintenance_window_end' in merged_config:
        merged_config['contact_directory_auto_maintenance_window_end'] = coerce_auto_maintenance_window_time(
            merged_config.get('contact_directory_auto_maintenance_window_end'),
            '23:59',
        )
    merged_config.update(normalize_ai_material_outreach_config(merged_config))
    _drop_legacy_ai_material_outreach_fields(merged_config)


def _coerce_backup_chat_api_fields(merged_config):
    api_configs = merged_config.get('api_configs')
    if not isinstance(api_configs, list):
        api_configs = []

    try:
        primary_index = int(merged_config.get('api_index', 0))
    except (TypeError, ValueError):
        primary_index = 0
    if api_configs:
        primary_index = max(0, min(len(api_configs) - 1, primary_index))
    else:
        primary_index = 0
    merged_config['api_index'] = primary_index

    try:
        backup_index = int(merged_config.get('backup_chat_api_index', -1))
    except (TypeError, ValueError):
        backup_index = -1
    if (
        len(api_configs) < 2
        or backup_index < 0
        or backup_index >= len(api_configs)
        or backup_index == primary_index
    ):
        backup_index = -1
    merged_config['backup_chat_api_index'] = backup_index


def _normalize_api_config_items(merged_config):
    api_configs = merged_config.get('api_configs')
    if not isinstance(api_configs, list):
        return
    normalized = []
    for item in api_configs:
        if not isinstance(item, dict):
            continue
        clean = dict(item)
        clean['sdk'] = str(clean.get('sdk') or '').strip()
        clean['model'] = str(clean.get('model') or '').strip()
        clean['url'] = str(clean.get('url') or '').strip()
        clean['key'] = str(clean.get('key') or '').strip()
        if clean['sdk'] == 'OpenAI SDK':
            clean['api_protocol'] = normalize_api_protocol(clean.get('api_protocol'))
        else:
            clean.pop('api_protocol', None)
        clean['reasoning_effort'] = normalize_reasoning_effort(clean.get('reasoning_effort'))
        normalized.append(clean)
    merged_config['api_configs'] = normalized


def _coerce_dict_fields(merged_config):
    _normalize_api_config_items(merged_config)
    normalize_voice_reply_config(merged_config)
    # keyword_dict 支持：dict / JSON字符串 / list[{key, value}]
    if 'keyword_dict' in merged_config:
        kd = merged_config['keyword_dict']
        if isinstance(kd, dict):
            normalized_rules = {}
            for key, value in kd.items():
                rule = _normalize_keyword_rule_value(key, value)
                if not rule:
                    continue
                normalized_rules[join_keyword_terms(rule.get('keywords', []))] = rule
            merged_config['keyword_dict'] = normalized_rules
            kd = merged_config['keyword_dict']
        if isinstance(kd, str):
            try:
                obj = json.loads(kd)
                if isinstance(obj, dict):
                    normalized_rules = {}
                    for key, value in obj.items():
                        rule = _normalize_keyword_rule_value(key, value)
                        if not rule:
                            continue
                        normalized_rules[join_keyword_terms(rule.get('keywords', []))] = rule
                    merged_config['keyword_dict'] = normalized_rules
                    kd = merged_config['keyword_dict']
            except Exception:
                pass
        if isinstance(kd, list):
            out = {}
            for item in kd:
                if isinstance(item, dict):
                    key = str(item.get('key', '')).strip()
                    val = _normalize_keyword_rule_value(key, item.get('value', ''))
                    if val:
                        out[join_keyword_terms(val.get('keywords', []))] = val
            merged_config['keyword_dict'] = out
            kd = out
        # 其他情况回退空 dict
        if not isinstance(kd, dict):
            merged_config['keyword_dict'] = {}

    if 'new_friend_msg' in merged_config:
        merged_config['new_friend_msg'] = normalize_new_friend_welcome_messages(
            merged_config.get('new_friend_msg')
        )

    # group_api_map: 值必须为 int 接口索引，非法值自动过滤
    if 'group_api_map' in merged_config:
        gam = merged_config['group_api_map']
        if isinstance(gam, dict):
            clean = {}
            for k, v in gam.items():
                k = str(k).strip()
                try:
                    vi = int(v)
                    if k and vi >= 0:
                        clean[k] = vi
                except (ValueError, TypeError):
                    pass
            merged_config['group_api_map'] = clean
        else:
            merged_config['group_api_map'] = {}

    # chat_api_map: 同 group_api_map，适用于私聊白名单用户
    if 'chat_api_map' in merged_config:
        cam = merged_config['chat_api_map']
        if isinstance(cam, dict):
            clean = {}
            for k, v in cam.items():
                k = str(k).strip()
                try:
                    vi = int(v)
                    if k and vi >= -1:
                        clean[k] = vi
                except (ValueError, TypeError):
                    pass
            merged_config['chat_api_map'] = clean
        else:
            merged_config['chat_api_map'] = {}

    if 'chat_tts_map' in merged_config:
        ctm = merged_config['chat_tts_map']
        if isinstance(ctm, dict):
            clean = {}
            for k, v in ctm.items():
                k = str(k).strip()
                try:
                    vi = int(v)
                    if k and vi >= 0:
                        clean[k] = vi
                except (ValueError, TypeError):
                    pass
            merged_config['chat_tts_map'] = clean
        else:
            merged_config['chat_tts_map'] = {}

    if 'api_capability_map' in merged_config:
        merged_config['api_capability_map'] = sanitize_api_capability_map(
            merged_config['api_capability_map']
        )

    # material_source_pool_limit_map: 单个素材来源的可用池上限，范围 1~50
    if 'material_source_pool_limit_map' in merged_config:
        mspm = merged_config['material_source_pool_limit_map']
        if isinstance(mspm, dict):
            clean = {}
            for k, v in mspm.items():
                k = str(k).strip()
                try:
                    vi = int(v)
                    if k:
                        clean[k] = max(1, min(50, vi))
                except (ValueError, TypeError):
                    pass
            merged_config['material_source_pool_limit_map'] = clean
        else:
            merged_config['material_source_pool_limit_map'] = {}

    # prompt 映射：值为非空字符串（prompt 文件名）
    for prompt_map_field in ('chat_prompt_map',):
        if prompt_map_field in merged_config:
            cpm = merged_config[prompt_map_field]
            if isinstance(cpm, dict):
                clean = {}
                for k, v in cpm.items():
                    k = str(k).strip()
                    v = str(v).strip()
                    if k and v:
                        clean[k] = v
                merged_config[prompt_map_field] = clean
            else:
                merged_config[prompt_map_field] = {}

    # group_prompt_map: 同 chat_prompt_map，适用于群组
    if 'group_prompt_map' in merged_config:
        gpm = merged_config['group_prompt_map']
        if isinstance(gpm, dict):
            clean = {}
            for k, v in gpm.items():
                k = str(k).strip()
                v = str(v).strip()
                if k and v:
                    clean[k] = v
            merged_config['group_prompt_map'] = clean
        else:
            merged_config['group_prompt_map'] = {}


def _normalize_custom_forward_rules(merged_config):
    rules = merged_config.get('custom_forward_list')
    if not isinstance(rules, list):
        return
    normalized = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rule_type = str(rule.get('type') or 'keyword').strip()
        if rule_type not in {'keyword', 'all'}:
            rule_type = 'keyword'
        item = {
            'id': rule.get('id'),
            'enabled': bool(rule.get('enabled', True)),
            'type': rule_type,
            'sources': [
                str(source).strip()
                for source in (rule.get('sources') or [])
                if str(source or '').strip()
            ],
            'keywords': normalize_keyword_terms(
                '；'.join(str(keyword or '') for keyword in (rule.get('keywords') or []))
                if isinstance(rule.get('keywords'), list)
                else rule.get('keywords')
            ),
            'targets': [
                str(target).strip()
                for target in (rule.get('targets') or [])
                if str(target or '').strip()
            ],
            'forward_with_source': bool(rule.get('forward_with_source', False)),
            'pause_ai_reply_on_match': bool(rule.get('pause_ai_reply_on_match', False)) if rule_type == 'keyword' else False,
            'forward_group_friend_messages': bool(rule.get('forward_group_friend_messages', False)) if rule_type == 'all' else False,
        }
        if not item['id']:
            item.pop('id', None)
        item['enabled'] = bool(item.get('enabled', True))
        normalized.append(item)
    merged_config['custom_forward_list'] = normalized


# 保存配置文件
last_save_config_error = ""


def save_config(config_data):
    global last_save_config_error
    last_save_config_error = ""
    try:
        original_config = read_config() or {}
        merged_config = {**original_config, **config_data}
        merged_config.setdefault('memory_context_switch', merged_config.get('memory_switch', True))

        _coerce_bool_fields(merged_config)
        _coerce_list_fields(merged_config)
        _coerce_float_fields(merged_config)
        _coerce_int_range_fields(merged_config)
        _coerce_backup_chat_api_fields(merged_config)
        _coerce_dict_fields(merged_config)
        merged_config['new_friend_reply_switch'] = new_friend_welcome_message_has_content(merged_config.get('new_friend_msg'))
        if 'keyword_dict' in (config_data or {}):
            _validate_keyword_rules_have_content(merged_config.get('keyword_dict', {}))
        _normalize_custom_forward_rules(merged_config)
        merged_config.update(normalize_ai_material_outreach_config(merged_config))
        _drop_legacy_ai_material_outreach_fields(merged_config)
        _validate_ai_material_outreach_config(merged_config)
        _normalize_schedule_task_lists(merged_config)
        if 'scheduled_message_task_list' in (config_data or {}):
            _validate_scheduled_message_tasks_have_content(merged_config.get('scheduled_message_task_list', []))
        if merged_config.get('text_reply_limit_ai_reply'):
            merged_config['text_reply_limit_reply'] = ''
        explicit_task_scope_wx_id = str((config_data or {}).get('task_scope_wx_id', '') or '').strip()
        account_wx_id = _validated_task_scope_wx_id(explicit_task_scope_wx_id)
        if 'keyword_dict' in merged_config:
            if 'keyword_dict' in (config_data or {}) and not account_wx_id:
                raise ValueError('保存关键词回复规则前，必须先确定当前微信号')
            if account_wx_id:
                _save_account_scoped_keyword_rules(merged_config.get('keyword_dict', {}), account_wx_id)
            merged_config.pop('keyword_dict', None)
        if 'custom_forward_list' in merged_config:
            if 'custom_forward_list' in (config_data or {}) and not account_wx_id:
                raise ValueError('保存自定义转发规则前，必须先确定当前微信号')
            if account_wx_id:
                _save_account_scoped_custom_forward_rules(
                    merged_config.get('custom_forward_list', []),
                    account_wx_id,
                )
            merged_config.pop('custom_forward_list', None)
        if 'scheduled_message_task_list' in merged_config:
            if 'scheduled_message_task_list' in (config_data or {}) and not account_wx_id:
                raise ValueError('保存定时消息任务前，必须先确定当前微信号')
            if account_wx_id:
                normalized_scheduled_tasks = _save_account_scoped_scheduled_message_tasks(
                    merged_config.get('scheduled_message_task_list', []),
                    account_wx_id,
                )
                _cancel_disabled_scheduled_message_runtime_instances(
                    normalized_scheduled_tasks,
                    account_wx_id,
                )
            merged_config.pop('scheduled_message_task_list', None)
        if 'material_outreach_list' in merged_config:
            if 'material_outreach_list' in (config_data or {}) and not account_wx_id:
                raise ValueError('保存素材转发任务前，必须先确定当前微信号')
            if account_wx_id:
                normalized_material_tasks = _save_account_scoped_material_outreach_tasks(
                    merged_config.get('material_outreach_list', []),
                    account_wx_id,
                )
                _cancel_disabled_material_outreach_runtime_instances(
                    normalized_material_tasks,
                    account_wx_id,
                )
            merged_config.pop('material_outreach_list', None)
        if 'moments_task_list' in merged_config:
            if 'moments_task_list' in (config_data or {}) and not account_wx_id:
                raise ValueError('保存朋友圈任务前，必须先确定当前微信号')
            if account_wx_id:
                _save_moments_tasks(
                    merged_config.get('moments_task_list', []),
                    account_wx_id,
                )
            merged_config.pop('moments_task_list', None)
        merged_config.pop('task_scope_wx_id', None)
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(merged_config, f, ensure_ascii=False, indent=4)
        log('SUCCESS', '配置文件保存成功')
        return True
    except Exception as e:
        last_save_config_error = str(e) or "未知错误"
        log('ERROR', f'保存配置文件失败: {last_save_config_error}')
        return False

#   保存配置
update_config_status = False # 记录是否更新了定时启停状态
@app.route('/save_config', methods=['POST'])
@login_required
def save_config_route():
    try:
        config_data = request.get_json()
        if not config_data:
            return jsonify({'status': 'error', 'message': '无效的配置数据'})

        merged_config = {**(read_config() or {}), **config_data}
        merged_config.setdefault('memory_context_switch', merged_config.get('memory_switch', True))

        # 预处理（与 save_config 二次校验互补）
        _coerce_bool_fields(merged_config)
        _coerce_list_fields(merged_config)
        _coerce_float_fields(merged_config)
        _coerce_int_range_fields(merged_config)
        _coerce_backup_chat_api_fields(merged_config)
        _coerce_dict_fields(merged_config)
        if 'keyword_dict' in (config_data or {}):
            _validate_keyword_rules_have_content(merged_config.get('keyword_dict', {}))
        _normalize_custom_forward_rules(merged_config)
        merged_config.update(normalize_ai_material_outreach_config(merged_config))
        _drop_legacy_ai_material_outreach_fields(merged_config)
        _validate_ai_material_outreach_config(merged_config)
        _normalize_schedule_task_lists(merged_config)
        if 'scheduled_message_task_list' in (config_data or {}):
            _validate_scheduled_message_tasks_have_content(merged_config.get('scheduled_message_task_list', []))
        if merged_config.get('text_reply_limit_ai_reply'):
            merged_config['text_reply_limit_reply'] = ''
        if save_config(merged_config):
            global update_config_status
            update_config_status = True # 执行了保存配置
            if bot_thread and bot_thread.is_alive() and bot:
                api_runtime_fields = {
                    'api_configs',
                    'api_index',
                    'api_capability_map',
                    'backup_chat_api_index',
                    'backup_chat_api_failover_threshold',
                }
                if hasattr(bot, 'apply_runtime_api_config_update') and any(
                    field in (config_data or {}) for field in api_runtime_fields
                ):
                    try:
                        bot.apply_runtime_api_config_update(merged_config)
                    except Exception as e:
                        log('WARNING', f'运行中接口配置同步失败，将沿用旧接口直到下次重启/刷新：{e}')
                if hasattr(bot, 'request_runtime_task_reload'):
                    bot.request_runtime_task_reload()
                return jsonify({'status': 'success', 'message': '配置保存成功，运行中的机器人将自动同步新任务'})
            return jsonify({'status': 'success', 'message': '配置保存成功'})
        else:
            message = last_save_config_error or '配置保存失败'
            return jsonify({'status': 'error', 'message': message})
    except Exception as e:
        log('ERROR', f'保存配置出错: {str(e)}')
        return jsonify({'status': 'error', 'message': str(e)})


def _build_temp_api_config(cfg, *, interface_index=None):
    """用于测试单个接口配置的健康检查快照，不读写 config.json。"""
    return build_api_config_snapshot(
        cfg,
        prompt=API_TEXT_TEST_PROMPT,
        max_retries=0,
        max_output_tokens=API_TEST_MAX_OUTPUT_TOKENS,
        interface_index=interface_index,
    )


def _build_panel_generation_api_config(cfg, *, prompt=""):
    """用于面板短生成任务的接口快照，避免复用健康检查参数。"""
    return build_api_config_snapshot(
        cfg,
        prompt=prompt,
        max_retries=1,
        max_output_tokens=PANEL_GENERATION_MAX_OUTPUT_TOKENS,
    )


def _build_memory_extraction_api_config(cfg, *, prompt=""):
    """用于会话记忆提取的接口快照，JSON 输出比发圈候选给得更宽。"""
    return build_api_config_snapshot(
        cfg,
        prompt=prompt,
        max_retries=1,
        max_output_tokens=MEMORY_EXTRACTION_MAX_OUTPUT_TOKENS,
    )


def _build_test_api_client(tmp_config):
    sdk = tmp_config.sdk
    if sdk == "OpenAI SDK":
        return OpenAIAPI(tmp_config)
    if sdk == "DusAPI":
        return DusAPI(tmp_config)
    raise ValueError(f"不支持的聊天接口 SDK：{sdk or '（空）'}")


def _get_active_api_config(config):
    config = config if isinstance(config, dict) else {}
    api_configs = config.get('api_configs')
    if isinstance(api_configs, list) and api_configs:
        try:
            index = int(config.get('api_index', 0))
        except (TypeError, ValueError):
            index = 0
        index = max(0, min(len(api_configs) - 1, index))
        cfg = api_configs[index]
        return cfg if isinstance(cfg, dict) else {}
    return {}


def _get_chat_api_config(config, chat_name):
    config = config if isinstance(config, dict) else {}
    api_configs = config.get('api_configs')
    if not isinstance(api_configs, list) or not api_configs:
        return _get_active_api_config(config)
    try:
        index = int(config.get('api_index', 0))
    except (TypeError, ValueError):
        index = 0
    listen_list = config.get('listen_list', []) or []
    chat_api_map = config.get('chat_api_map', {}) or {}
    if (
        isinstance(listen_list, list)
        and chat_name in listen_list
        and isinstance(chat_api_map, dict)
        and chat_name in chat_api_map
    ):
        try:
            index = int(chat_api_map.get(chat_name))
        except (TypeError, ValueError):
            pass
    index = max(0, min(len(api_configs) - 1, index))
    cfg = api_configs[index]
    return cfg if isinstance(cfg, dict) else {}


def _png_chunk(chunk_type, data):
    payload = chunk_type + data
    return (
        struct.pack(">I", len(data))
        + payload
        + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)
    )


def _build_rgb_png(width, height, pixels):
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)
        start = y * stride
        raw.extend(pixels[start:start + stride])
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


@app.route('/api/tts/preview-file/<path:filename>', methods=['GET'])
@login_required
def api_tts_preview_file(filename):
    safe_name = os.path.basename(str(filename or ''))
    if not safe_name:
        return jsonify({'status': 'error', 'message': '文件不存在'}), 404
    target_path = os.path.join(DATA_DIR, 'cache', 'tts_preview', safe_name)
    if not os.path.exists(target_path):
        return jsonify({'status': 'error', 'message': '文件不存在'}), 404
    return send_file(target_path, mimetype='audio/mpeg')


@app.route('/api/tts/preview', methods=['POST'])
@login_required
def api_tts_preview():
    payload = request.get_json(silent=True) or {}
    try:
        payload = resolve_tts_preview_payload(payload, saved_config=normalize_tts_settings(read_config() or {}))
        cache_dir = os.path.join(DATA_DIR, 'cache', 'tts_preview')
        client = create_tts_client(payload)
        out_path = make_tts_cache_path(cache_dir, suffix='mp3')
        client.synthesize(payload.get('sample_text') or '你好呀，这是一条语音回复试听。', out_path)
        return jsonify({
            'status': 'success',
            'audio_url': f"/api/tts/preview-file/{out_path.name}",
        })
    except TTSConfigError as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 400
    except Exception as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 400

def _fill_rect(pixels, width, x1, y1, x2, y2, color):
    r, g, b = color
    for y in range(y1, y2):
        row = y * width * 3
        for x in range(x1, x2):
            idx = row + x * 3
            pixels[idx:idx + 3] = bytes((r, g, b))


def _build_vision_test_png():
    width, height = 520, 360
    pixels = bytearray([255] * width * height * 3)
    _fill_rect(pixels, width, 80, 50, 440, 310, (230, 40, 40))
    return _build_rgb_png(width, height, pixels)


def _validate_image_test_reply(reply):
    lower = str(reply or "").lower()
    missing_image_markers = (
        "no image",
        "can't see",
        "cannot see",
        "don't see",
        "not see",
        "upload",
        "provide",
        "无法",
        "没有图片",
        "看不到",
        "未提供",
    )
    if any(marker in lower for marker in missing_image_markers):
        return False
    return API_IMAGE_TEST_EXPECTED_COLOR in lower

def _run_api_image_test(api, sdk):
    """用可校验内容 PNG 测试当前接口是否真正支持图片输入。"""
    if sdk not in ("OpenAI SDK", "DusAPI"):
        return {
            'status': 'skipped',
            'message': '当前接口类型暂不支持通用图片测试'
        }
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as f:
            f.write(_build_vision_test_png())
            tmp_path = f.name
        reply = api.chat(
            API_IMAGE_TEST_MESSAGE,
            stream=False,
            prompt=API_IMAGE_TEST_PROMPT,
            history=[],
            image_path=tmp_path,
        )
        raw_reply = str(reply or "")
        cleaned_reply = clean_ai_reply_text(raw_reply)
        if not raw_reply or raw_reply == "API返回错误，请稍后再试":
            return {
                'status': 'error',
                'message': '图片测试未返回有效文本，请确认模型支持视觉输入'
            }
        if not _validate_image_test_reply(cleaned_reply):
            return {
                'status': 'error',
                'message': '图片测试未读出测试图片中的红色方块，请确认模型/接口真正支持视觉输入',
                'reply': cleaned_reply or '（清洗后为空）',
                'raw_length': len(raw_reply),
                'cleaned': cleaned_reply != raw_reply,
            }
        return {
            'status': 'success',
            'reply': cleaned_reply or '（清洗后为空）',
            'raw_length': len(raw_reply),
            'cleaned': cleaned_reply != raw_reply,
        }
    except TypeError as e:
        return {
            'status': 'skipped',
            'message': f'当前接口类暂不支持图片参数：{e}'
        }
    except Exception as e:
        msg = str(e)
        if len(msg) > 500:
            msg = msg[:500] + '...'
        return {
            'status': 'error',
            'message': f'图片测试失败：{msg}'
        }
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _set_api_capability(config, index, capability, supported):
    return set_api_capability(config, index, capability, supported)


def _persist_api_capability(index, capability, supported):
    config = read_config()
    if not isinstance(config, dict):
        return False
    _set_api_capability(config, index, capability, supported)
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
        return True
    except Exception as e:
        log('WARNING', f'写入接口能力配置失败: {e}')
        return False


def _get_openai_protocol_test_result(api, sdk):
    if sdk != "OpenAI SDK":
        return None
    status = getattr(api, 'last_protocol_status', None)
    if not isinstance(status, dict):
        return None
    value = status.get('status')
    labels = {
        'responses_ok': 'Responses API 可用',
        'chat_completions_ok': 'Chat Completions 可用',
        'failed': '失败',
    }
    label = labels.get(value)
    if not label:
        return None
    return {
        'status': value,
        'label': label,
    }


@app.route('/test_api_config', methods=['POST'])
@login_required
def test_api_config_route():
    started = time.time()
    try:
        data = request.get_json() or {}
        cfg = data.get('api_config') or {}
        try:
            api_index = int(data.get('api_index', 0))
        except (TypeError, ValueError):
            api_index = 0
        if not isinstance(cfg, dict):
            return jsonify({'status': 'error', 'message': '接口配置格式无效'})

        tmp_config = _build_temp_api_config(cfg, interface_index=api_index)
        if not tmp_config.key:
            return jsonify({'status': 'error', 'message': 'API Key 不能为空'})
        if not tmp_config.url:
            return jsonify({'status': 'error', 'message': 'Base URL 不能为空'})
        if not tmp_config.model:
            return jsonify({'status': 'error', 'message': '模型名称不能为空'})

        api = _build_test_api_client(tmp_config)
        text_started = time.time()
        reply = api.chat(API_TEST_MESSAGE, stream=False, prompt=tmp_config.prompt, history=[])
        text_elapsed_ms = int((time.time() - text_started) * 1000)
        log('INFO', f'接口测试文本测试完成：接口 {api_index + 1}，耗时 {text_elapsed_ms} ms')
        raw_reply = str(reply or "")
        cleaned_reply = clean_ai_reply_text(raw_reply)
        cleaned = cleaned_reply != raw_reply
        protocol_test = _get_openai_protocol_test_result(api, tmp_config.sdk)

        if not raw_reply or raw_reply == "API返回错误，请稍后再试":
            return jsonify({
                'status': 'error',
                'message': '接口有响应，但未返回有效文本，请检查模型名称、接口地址或服务商兼容性'
            })

        image_started = time.time()
        image_test = _run_api_image_test(api, tmp_config.sdk)
        image_elapsed_ms = int((time.time() - image_started) * 1000)
        log('INFO', f'接口测试图片测试完成：接口 {api_index + 1}，结果 {image_test.get("status")}，耗时 {image_elapsed_ms} ms')
        _persist_api_capability(api_index, 'vision', image_test.get('status') == 'success')
        elapsed_ms = int((time.time() - started) * 1000)
        return jsonify({
            'status': 'success',
            'data': {
                'reply': cleaned_reply or '（清洗后为空：接口可能只返回了思考内容）',
                'raw_length': len(raw_reply),
                'cleaned': cleaned,
                'elapsed_ms': elapsed_ms,
                'text_elapsed_ms': text_elapsed_ms,
                'image_elapsed_ms': image_elapsed_ms,
                'image_test': image_test,
                'protocol_status': protocol_test.get('status') if protocol_test else '',
                'protocol_label': protocol_test.get('label') if protocol_test else '',
            }
        })
    except Exception as e:
        msg = str(e)
        if len(msg) > 800:
            msg = msg[:800] + '...'
        return jsonify({'status': 'error', 'message': f'接口测试失败：{msg}'})


# ----------------------------------------------------------
# Prompt 文件管理路由
# ----------------------------------------------------------

@app.route('/list_prompts')
@login_required
def list_prompts_route():
    return jsonify({'status': 'success', 'prompts': _get_prompts_list()})

@app.route('/save_prompt', methods=['POST'])
@login_required
def save_prompt_route():
    import tempfile
    try:
        data = request.get_json()
        if not data:
            return jsonify({'status': 'error', 'msg': '无效请求'})
        name     = _normalize_prompt_file_name(data.get('name', ''))
        content  = str(data.get('content', ''))
        old_name = _normalize_prompt_file_name(data.get('old_name', ''))
        if not name:
            return jsonify({'status': 'error', 'msg': '基础人设名称不能为空'})
        if _is_persona_status_name(name):
            return jsonify({'status': 'error', 'msg': f'基础人设名称不能以 {PERSONA_STATUS_SUFFIX} 结尾'})
        # 白名单校验：只允许中文/字母/数字/空格/下划线/连字符
        if not _is_valid_prompt_file_name(name):
            return jsonify({'status': 'error', 'msg': 'Prompt 名称含非法字符（只允许中文、字母、数字、空格、_ 和 -）'})
        _ensure_prompt_dir()
        # 重命名：删除旧文件
        if old_name and old_name != name:
            old_path = os.path.join(PROMPT_DIR, f'{old_name}.md')
            if os.path.exists(old_path):
                os.remove(old_path)
            old_status_path = _persona_status_path(old_name)
            new_status_path = _persona_status_path(name)
            if os.path.exists(old_status_path):
                if not os.path.exists(new_status_path):
                    os.replace(old_status_path, new_status_path)
                else:
                    os.remove(old_status_path)
        # 原子写入
        target = os.path.join(PROMPT_DIR, f'{name}.md')
        tmp_fd, tmp_path = tempfile.mkstemp(dir=PROMPT_DIR, suffix='.tmp')
        try:
            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as tf:
                tf.write(content)
            os.replace(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
        log('SUCCESS', f'Prompt 已保存：{name}.md')
        return jsonify({'status': 'success'})
    except Exception as e:
        log('ERROR', f'保存 Prompt 失败: {e}')
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/delete_prompt', methods=['POST'])
@login_required
def delete_prompt_route():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'status': 'error', 'msg': '无效请求'})
        name = _normalize_prompt_file_name(data.get('name', ''))
        if not name:
            return jsonify({'status': 'error', 'msg': '名称不能为空'})
        if _is_persona_status_name(name):
            return jsonify({'status': 'error', 'msg': '不能把人设近况当作基础人设删除'})
        _ensure_prompt_dir()
        # 不允许删除最后一个
        md_files = [
            f for f in os.listdir(PROMPT_DIR)
            if f.endswith('.md') and not _is_persona_status_filename(f)
        ]
        if len(md_files) <= 1:
            return jsonify({'status': 'error', 'msg': '不允许删除最后一个 Prompt'})
        config = read_config() or {}
        prompt_refs = []
        if str(config.get('default_prompt') or '').strip() == name:
            prompt_refs.append('默认人设')
        for field_name, label in (
            ('chat_prompt_map', '私聊'),
            ('group_prompt_map', '群聊'),
        ):
            bindings = config.get(field_name)
            if not isinstance(bindings, dict):
                continue
            for target_name, prompt_name in bindings.items():
                if str(prompt_name or '').strip() != name:
                    continue
                target_name = str(target_name or '').strip()
                prompt_refs.append(f'{label}「{target_name}」' if target_name else label)
        if prompt_refs:
            return jsonify({
                'status': 'error',
                'msg': '该基础人设仍在使用，不能删除：' + '、'.join(prompt_refs[:8]),
            }), 400
        target = os.path.join(PROMPT_DIR, f'{name}.md')
        if os.path.exists(target):
            os.remove(target)
        status_target = _persona_status_path(name)
        if os.path.exists(status_target):
            os.remove(status_target)
        log('SUCCESS', f'Prompt 已删除：{name}.md')
        return jsonify({'status': 'success'})
    except Exception as e:
        log('ERROR', f'删除 Prompt 失败: {e}')
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/persona_status/<prompt_name>', methods=['GET', 'POST'])
@login_required
def persona_status_route(prompt_name):
    try:
        prompt_name = _normalize_prompt_file_name(prompt_name)
        if not prompt_name:
            return jsonify({'status': 'error', 'message': '人设名称不能为空'}), 400
        if _is_persona_status_name(prompt_name):
            return jsonify({'status': 'error', 'message': '请选择普通基础人设'}), 400
        if not _is_valid_prompt_file_name(prompt_name):
            return jsonify({'status': 'error', 'message': '人设名称含非法字符'}), 400
        _ensure_prompt_dir()
        path = _persona_status_path(prompt_name)
        if request.method == 'GET':
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    return jsonify({
                        'status': 'success',
                        'exists': True,
                        'content': f.read(),
                    })
            return jsonify({
                'status': 'success',
                'exists': False,
                'content': _get_default_persona_status_content(),
            })

        data = request.get_json() or {}
        content = str(data.get('content', ''))
        if not content.strip() or not _persona_status_has_usable_items(content):
            if os.path.exists(path):
                os.remove(path)
            log('SUCCESS', f'人设近况已清空：{prompt_name}')
            return jsonify({
                'status': 'success',
                'action': 'deleted',
                'exists': False,
                'content': _get_default_persona_status_content(),
            })

        tmp_fd, tmp_path = tempfile.mkstemp(dir=PROMPT_DIR, suffix='.tmp')
        try:
            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as tf:
                tf.write(content)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
        log('SUCCESS', f'人设近况已保存：{prompt_name}{PERSONA_STATUS_SUFFIX}.md')
        return jsonify({
            'status': 'success',
            'action': 'saved',
            'exists': True,
            'content': content,
        })
    except Exception as e:
        log('ERROR', f'保存人设近况失败: {e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500

# 启动/停止机器人
bot = None
bot_thread = None
relationship_full_scan_thread = None
relationship_full_scan_thread_lock = threading.Lock()
bot_stop_requested = threading.Event()
BOT_STOP_WAIT_TIMEOUT_SECONDS = 10
BOT_START_WAIT_TIMEOUT_SECONDS = 8
BOT_START_PENDING_MESSAGE = '正在连接微信，请稍候'
_VALID_BOT_STARTUP_STATUSES = {'idle', 'pending', 'success', 'error'}
bot_startup_state_lock = threading.Lock()
bot_startup_state = {
    'status': 'idle',
    'message': '机器人未启动',
}

# ============================================================
# 防锁屏 / 防睡眠工具函数（Windows SetThreadExecutionState）
# ============================================================
_ES_CONTINUOUS       = 0x80000000
_ES_SYSTEM_REQUIRED  = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002

def _prevent_sleep():
    """阻止 Windows 自动锁屏、黑屏、睡眠，机器人运行期间保持系统唤醒状态"""
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED
        )
        log('INFO', '【防锁屏】已阻止 Windows 自动锁屏/黑屏/睡眠，避免影响微信自动化操作')
    except Exception as e:
        log('WARNING', f'【防锁屏】设置防睡眠状态失败: {e}')

def _restore_sleep():
    """恢复 Windows 原有的锁屏、黑屏、睡眠策略"""
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        log('INFO', '【防锁屏】已恢复 Windows 原有锁屏/黑屏/睡眠策略')
    except Exception as e:
        log('WARNING', f'【防锁屏】恢复睡眠策略失败: {e}')

# 服务器进程异常退出时兜底恢复
atexit.register(_restore_sleep)


def _clear_bot_runtime_refs():
    global bot_thread, bot
    bot_thread = None
    bot = None


def _normalize_bot_startup_state(status=None, message=None):
    normalized_status = str(status or 'idle').strip().lower()
    if normalized_status not in _VALID_BOT_STARTUP_STATUSES:
        normalized_status = 'idle'
    default_messages = {
        'idle': '机器人未启动',
        'pending': BOT_START_PENDING_MESSAGE,
        'success': '机器人已启动',
        'error': '机器人启动失败',
    }
    return {
        'status': normalized_status,
        'message': str(message or default_messages[normalized_status]),
    }


def _set_bot_startup_state(status, message=None):
    snapshot = _normalize_bot_startup_state(status, message)
    with bot_startup_state_lock:
        bot_startup_state.clear()
        bot_startup_state.update(snapshot)
    return dict(snapshot)


def _get_bot_startup_state_snapshot():
    with bot_startup_state_lock:
        snapshot = dict(bot_startup_state)
    return _normalize_bot_startup_state(snapshot.get('status'), snapshot.get('message'))


def _report_bot_startup_state(success, message, event=None, state=None):
    snapshot = _set_bot_startup_state('success' if success else 'error', message)
    if isinstance(state, dict):
        state.clear()
        state.update(snapshot)
    if event is not None and not event.is_set():
        event.set()
    return snapshot


def _run_bot_worker(startup_event=None, startup_state=None):
    global bot
    startup_state = startup_state if isinstance(startup_state, dict) else {}
    pythoncom.CoInitialize()
    try:
        # 启动前先清理旧实例的残留监听，防止崩溃重启后同一群/用户被双重注册导致双回调
        if bot:
            try:
                bot.stop()
                log('INFO', '已清理上次残留的 WeChat 监听')
            except Exception as _e:
                log('WARNING', f'清理旧监听时出错（可忽略）: {_e}')
        bot = WXBot()
        if bot_stop_requested.is_set():
            try:
                bot.stop_wxbot()
            except Exception as _e:
                log('WARNING', f'启动前停止机器人时出错（可忽略）: {_e}')
            _report_bot_startup_state(False, '机器人启动过程中已被停止', startup_event, startup_state)
            return
        bot._startup_callback = _startup_status_callback(startup_event, startup_state)
        bot.run()
        snapshot = _get_bot_startup_state_snapshot()
        if snapshot.get('status') == 'pending':
            _report_bot_startup_state(False, '机器人启动后立即退出，请查看日志确认微信和授权状态', startup_event, startup_state)
    except Exception as e:
        _report_bot_startup_state(False, f'机器人启动失败：{e}', startup_event, startup_state)
        log('ERROR', f'启动机器人失败: {e}')
    finally:
        pythoncom.CoUninitialize()
        _restore_sleep()


def _start_bot_runtime(wait_timeout=None):
    global bot_thread
    bot_stop_requested.clear()
    startup_event = threading.Event() if wait_timeout else None
    startup_state = {}
    _set_bot_startup_state('pending', BOT_START_PENDING_MESSAGE)
    bot_thread = threading.Thread(
        target=lambda: _run_bot_worker(startup_event, startup_state),
        daemon=True,
    )
    bot_thread.start()
    if startup_event is None:
        _prevent_sleep()
        snapshot = _get_bot_startup_state_snapshot()
        if snapshot.get('status') == 'error':
            _clear_bot_runtime_refs()
        return snapshot
    if startup_event.wait(wait_timeout):
        snapshot = _normalize_bot_startup_state(startup_state.get('status'), startup_state.get('message'))
        if snapshot.get('status') == 'error':
            _clear_bot_runtime_refs()
            return snapshot
        _prevent_sleep()
        return snapshot
    _prevent_sleep()
    return _get_bot_startup_state_snapshot()


def _stop_running_bot_and_wait(wait_timeout=BOT_STOP_WAIT_TIMEOUT_SECONDS):
    global bot_thread, bot
    bot_stop_requested.set()
    thread = bot_thread
    current_bot = bot
    if not thread or not thread.is_alive():
        bot_stop_requested.clear()
        _clear_bot_runtime_refs()
        _restore_sleep()
        return False, '机器人未运行'
    if not current_bot or not hasattr(current_bot, 'stop_wxbot'):
        try:
            thread.join(wait_timeout)
        except Exception as e:
            log('ERROR', f'等待启动中机器人停止时出错: {e}')
            return False, f'等待机器人停止失败：{e}'
        if thread.is_alive():
            log('WARNING', '机器人仍在启动/停止交界中，请稍后重试')
            return None, '机器人仍在停止中，请稍后再试'
        bot_stop_requested.clear()
        _clear_bot_runtime_refs()
        _set_bot_startup_state('idle', '机器人未启动')
        _restore_sleep()
        log('SUCCESS', '机器人已停止')
        return True, '机器人已停止'
    if not current_bot.stop_wxbot():
        log('ERROR', '停止机器人失败')
        return False, '停止机器人失败'
    try:
        thread.join(wait_timeout)
    except Exception as e:
        log('ERROR', f'等待机器人停止时出错: {e}')
        return False, f'等待机器人停止失败：{e}'
    if thread.is_alive():
        log('WARNING', '机器人仍在停止中，请稍后重试')
        return None, '机器人仍在停止中，请稍后再试'
    _clear_bot_runtime_refs()
    bot_stop_requested.clear()
    _set_bot_startup_state('idle', '机器人未启动')
    log('SUCCESS', '机器人已停止')
    return True, '机器人已停止'


def _startup_status_callback(event, state):
    def mark(success, message):
        _report_bot_startup_state(success, message, event, state)
    return mark

@app.route('/start_bot', methods=['POST'])
@login_required
def start_bot():
    log('INFO', '机器人启动请求已接收')
    global bot_thread
    if bot_thread and bot_thread.is_alive():
        snapshot = _get_bot_startup_state_snapshot()
        if snapshot.get('status') == 'pending':
            log('INFO', '状态：机器人仍在启动中')
            return jsonify(snapshot)
        log("WARNING", "状态：机器人已在运行")
        return jsonify({'status': 'success', 'message': '机器人已在运行'})
    try:
        return jsonify(_start_bot_runtime())
    except Exception as e:
        log('ERROR', f'启动机器人失败: {str(e)}')
        return jsonify({'status': 'error', 'message': f'机器人启动失败：{e}'})


@app.route('/get_startup_status')
@login_required
def get_startup_status():
    return jsonify(_get_bot_startup_state_snapshot())

@app.route('/stop_bot', methods=['POST'])
@login_required
def stop_bot():
    log('INFO', '机器人停止请求已接收')
    global bot_thread, bot
    if bot_thread and bot_thread.is_alive():
        ok, message = _stop_running_bot_and_wait()
        if ok:
            return jsonify({'status': 'success', 'message': message})
        if ok is None:
            return jsonify({'status': 'stopping', 'message': message})
        return jsonify({'status': 'error', 'message': message})
    else:
        log('WARNING', '状态：机器人未运行')
        return jsonify({'status': 'error', 'message': '机器人未运行'})

@app.route('/check_activate')
@login_required
def check_activate():
    try:
        from wxautox4.utils.useful import check_license
        activated = check_license()
        return jsonify({'status': 'success', 'data': {
            'activated': bool(activated),
            'wxautox4_version': _get_wxautox_version(),
        }})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


def _get_wxautox_version():
    try:
        return importlib.metadata.version('wxautox4')
    except Exception:
        try:
            import wxautox4
            return getattr(wxautox4, '__version__', '') or ''
        except Exception:
            return ''


def _completed_output(completed):
    return ((completed.stdout or '') + '\n' + (completed.stderr or '')).strip()


def _run_wxautox_license_check():
    script = (
        "from wxautox4.utils.useful import check_license\n"
        "import sys\n"
        "sys.exit(0 if check_license() else 2)\n"
    )
    try:
        completed = subprocess.run(
            [sys.executable, '-c', script],
            capture_output=True,
            text=True,
            timeout=60,
            encoding='utf-8',
            errors='replace',
        )
        return completed.returncode == 0, _completed_output(completed)
    except Exception as e:
        return False, str(e)


def _normalize_version_for_compare(value):
    text = str(value or '').strip()
    if not text:
        return ''
    text = text.split()[0].strip()
    if text[:1].lower() == 'v':
        text = text[1:]
    return text.strip()


def _version_update_available(local_version, remote_version):
    local_norm = _normalize_version_for_compare(local_version)
    remote_norm = _normalize_version_for_compare(remote_version)
    if not local_norm or not remote_norm:
        return False
    return local_norm != remote_norm


def _parse_latest_wxautox_version_from_pip_output(output):
    text = str(output or '')
    for pattern in (
        r'(?im)^\s*LATEST:\s*([^\s,]+)',
        r'(?im)^\s*wxautox4\s*\(([^)\s]+)\)',
        r'(?im)Available versions:\s*([^\s,]+)',
    ):
        match = re.search(pattern, text)
        if match:
            return str(match.group(1) or '').strip()
    return ''


def _get_latest_wxautox_version():
    """Return (ok, latest_version, diagnostic_output) using the active pip index config."""
    cmd = [sys.executable, '-m', 'pip', 'index', 'versions', 'wxautox4']
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            encoding='utf-8',
            errors='replace',
        )
        output = _completed_output(completed)
        if completed.returncode != 0:
            return False, '', output or 'pip index versions wxautox4 执行失败'
        latest_version = _parse_latest_wxautox_version_from_pip_output(output)
        if not latest_version:
            return False, '', output or '未能从 pip 输出中解析 wxautox4 最新版本'
        return True, latest_version, output
    except subprocess.TimeoutExpired:
        return False, '', '检查 wxautox4 最新版本超时'
    except Exception as e:
        return False, '', str(e)


def _run_wxautox_rollback(previous_version):
    if not previous_version:
        return False, '未能读取旧版本号，无法自动回滚。'
    rollback_cmd = [sys.executable, '-m', 'pip', 'install', f'wxautox4=={previous_version}']
    rollback_completed = subprocess.run(
        rollback_cmd,
        capture_output=True,
        text=True,
        timeout=300,
        encoding='utf-8',
        errors='replace',
    )
    return rollback_completed.returncode == 0, _completed_output(rollback_completed)

@app.route('/activate', methods=['POST'])
@login_required
def activate():
    try:
        data = request.get_json()
        code = (data.get('code') or '').strip()
        if not code:
            return jsonify({'status': 'error', 'message': '激活码不能为空'})
        from wxautox4.utils.useful import authenticate
        result = authenticate(code)
        if result:
            log('SUCCESS', f'wxautox4 激活成功')
            return jsonify({'status': 'success', 'message': '激活成功！'})
        else:
            log('WARNING', f'wxautox4 激活失败，激活码无效或已过期')
            return jsonify({'status': 'error', 'message': '激活失败，激活码无效或已过期'})
    except Exception as e:
        log('ERROR', f'激活出错: {e}')
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/update_wxautox_kernel', methods=['POST'])
@login_required
def update_wxautox_kernel():
    """源码版在线更新 wxautox4 内核库。"""
    global bot_thread, bot
    try:
        if hasattr(sys, '_MEIPASS'):
            return jsonify({
                'status': 'error',
                'message': '打包版不支持在线更新内核，请下载新版程序包后替换。'
            })

        previous_version = _get_wxautox_version()
        if not previous_version:
            log('ERROR', '检查 wxautox4 当前版本失败：未能读取本地版本号')
            return jsonify({
                'status': 'error',
                'message': '检查本地内核版本失败，无法判断是否需要更新。请确认 wxautox4 已正确安装后再重试。',
                'previous_version': previous_version,
                'current_version': previous_version,
                'update_available': False,
                'rolled_back': False,
                'stopped_bot': False,
            })
        latest_ok, latest_version, latest_output = _get_latest_wxautox_version()
        if not latest_ok:
            log('ERROR', f'检查 wxautox4 最新版本失败：{latest_output}')
            return jsonify({
                'status': 'error',
                'message': '检查内核最新版本失败，请稍后重试；如持续失败，请在终端手动执行 pip index versions wxautox4 查看详细错误。',
                'latest_output': latest_output,
                'previous_version': previous_version,
                'current_version': previous_version,
                'update_available': False,
                'rolled_back': False,
                'stopped_bot': False,
            })
        update_available = _version_update_available(previous_version, latest_version)
        if not update_available:
            log('INFO', f'wxautox4 当前已是最新版：{previous_version or latest_version}')
            return jsonify({
                'status': 'success',
                'message': '当前内核已是最新版，无需更新。',
                'latest_output': latest_output,
                'previous_version': previous_version,
                'current_version': previous_version,
                'latest_version': latest_version,
                'update_available': False,
                'rolled_back': False,
                'stopped_bot': False,
            })

        license_ok, license_output = _run_wxautox_license_check()
        if not license_ok:
            log('WARNING', f'wxautox4 更新前授权检测失败：{license_output}')
            return jsonify({
                'status': 'error',
                'message': '当前内核授权已过期或不可用，继续更新可能导致授权失效。请先续期或重新激活授权后再升级。',
                'latest_output': latest_output,
                'license_output': license_output,
                'previous_version': previous_version,
                'current_version': previous_version,
                'latest_version': latest_version,
                'update_available': True,
                'license_expired': True,
                'rolled_back': False,
                'stopped_bot': False,
            })

        stopped_bot = False
        if bot_thread and bot_thread.is_alive():
            try:
                stopped_ok, stop_message = _stop_running_bot_and_wait()
                if not stopped_ok:
                    log('ERROR', f'更新 wxautox4 前自动停止机器人失败：{stop_message}')
                    return jsonify({
                        'status': 'error',
                        'message': f'停止机器人失败，请手动停止后重试。{stop_message}'
                    })
                log('SUCCESS', '更新 wxautox4 前已自动停止机器人')
                stopped_bot = True
            except Exception as e:
                log('ERROR', f'更新 wxautox4 前自动停止机器人出错: {e}')
                return jsonify({
                    'status': 'error',
                    'message': f'停止机器人失败，请手动停止后重试。错误信息：{e}'
                })

        cmd = [sys.executable, '-m', 'pip', 'install', '--upgrade', 'wxautox4']
        log('INFO', '开始更新 wxautox4 内核库')
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            encoding='utf-8',
            errors='replace',
        )
        output = _completed_output(completed)
        if completed.returncode == 0:
            license_ok, license_output = _run_wxautox_license_check()
            current_version = _get_wxautox_version()
            if not license_ok:
                log('ERROR', f'wxautox4 更新后授权检测失败：{license_output}')
                if previous_version:
                    rolled_back, rollback_output = _run_wxautox_rollback(previous_version)
                    if rolled_back:
                        log('SUCCESS', f'wxautox4 授权检测失败，已自动回滚到 {previous_version}')
                        return jsonify({
                            'status': 'error',
                            'message': '内核更新失败，已自动回滚。<br>请重启程序使回滚后版本生效，并确认授权仍在更新期内后再尝试升级。',
                            'output': output,
                            'latest_output': latest_output,
                            'license_output': license_output,
                            'rollback_output': rollback_output,
                            'rolled_back': True,
                            'previous_version': previous_version,
                            'current_version': previous_version,
                            'latest_version': latest_version,
                            'update_available': True,
                            'stopped_bot': stopped_bot,
                        })
                    log('ERROR', f'wxautox4 授权检测失败且自动回滚失败：{rollback_output}')
                    return jsonify({
                        'status': 'error',
                        'message': f'内核更新后授权检测失败，并且自动回滚到 {previous_version} 失败。请手动执行 pip install wxautox4=={previous_version}。',
                        'output': output,
                        'latest_output': latest_output,
                        'license_output': license_output,
                        'rollback_output': rollback_output,
                        'rolled_back': False,
                        'previous_version': previous_version,
                        'current_version': current_version,
                        'latest_version': latest_version,
                        'update_available': True,
                        'stopped_bot': stopped_bot,
                    })
                return jsonify({
                    'status': 'error',
                    'message': '内核更新后授权检测失败，但未能读取旧版本号，无法自动回滚。请手动安装可用的 wxautox4 旧版本。',
                    'output': output,
                    'latest_output': latest_output,
                    'license_output': license_output,
                    'rolled_back': False,
                    'previous_version': previous_version,
                    'current_version': current_version,
                    'latest_version': latest_version,
                    'update_available': True,
                    'stopped_bot': stopped_bot,
                })

            log('SUCCESS', 'wxautox4 内核库更新命令执行成功，授权检测通过，请重启程序使其生效')
            return jsonify({
                'status': 'success',
                'message': '内核更新完成，授权检测通过。请重启程序使新版 wxautox4 生效。',
                'output': output,
                'latest_output': latest_output,
                'license_output': license_output,
                'rolled_back': False,
                'previous_version': previous_version,
                'current_version': current_version,
                'latest_version': latest_version,
                'update_available': True,
                'stopped_bot': stopped_bot,
            })
        log('ERROR', f'wxautox4 内核库更新失败：{output}')
        rolled_back = False
        rollback_output = ''
        if previous_version:
            try:
                rolled_back, rollback_output = _run_wxautox_rollback(previous_version)
                if rolled_back:
                    log('SUCCESS', f'wxautox4 更新失败，已自动回滚到 {previous_version}')
                else:
                    log('ERROR', f'wxautox4 更新失败且自动回滚失败：{rollback_output}')
            except Exception as rollback_error:
                rollback_output = str(rollback_error)
                log('ERROR', f'wxautox4 更新失败且自动回滚出错：{rollback_output}')
        return jsonify({
            'status': 'error',
            'message': '内核更新失败，已尝试自动回滚。请重启程序确认当前内核版本；如持续失败，请在终端手动执行 pip install --upgrade wxautox4 查看详细错误。',
            'output': output,
            'latest_output': latest_output,
            'rollback_output': rollback_output,
            'rolled_back': rolled_back,
            'previous_version': previous_version,
            'current_version': previous_version,
            'latest_version': latest_version,
            'update_available': True,
            'stopped_bot': stopped_bot,
        })
    except subprocess.TimeoutExpired:
        log('ERROR', 'wxautox4 内核库更新超时')
        return jsonify({'status': 'error', 'message': '内核更新超时，请稍后重试。'})
    except Exception as e:
        log('ERROR', f'更新 wxautox4 内核库出错: {e}')
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/check_update')
@login_required
def check_update():
    try:
        import requests as req
        import wxbot_core as wxbot_mod
        import uuid

        local_version = getattr(wxbot_mod, 'version', '')
        update_feed_url = str(getattr(wxbot_mod, 'update_feed_url', '') or 'https://wxbot.siverking.online/version.json').strip()
        update_source_name = str(getattr(wxbot_mod, 'update_source_name', '') or '官方版本源').strip()
        machine_code = hex(uuid.getnode())[2:].upper()
        headers = {
            'User-Agent': f'{machine_code}-{local_version}'
        }

        r = req.get(update_feed_url, headers=headers, timeout=60)
        data = r.json()
        if not isinstance(data, dict):
            return jsonify({'status': 'error', 'message': '版本信息格式异常：远端返回值不是 JSON 对象'})
        remote_version = str(data.get('version', '') or '').strip()
        if not remote_version:
            return jsonify({'status': 'error', 'message': '版本信息格式异常：缺少 version 字段'})

        data['local_version'] = local_version
        data['local_version_normalized'] = _normalize_version_for_compare(local_version)
        data['version_normalized'] = _normalize_version_for_compare(remote_version)
        data['update_available'] = _version_update_available(local_version, remote_version)
        data['machine_code'] = machine_code
        data['custom_build'] = bool(getattr(wxbot_mod, 'custom_build', False))
        data['update_feed_url'] = update_feed_url
        data['update_source_name'] = update_source_name
        data['source_repo_url'] = str(getattr(wxbot_mod, 'source_repo_url', '') or '')
        data['release_url'] = str(getattr(wxbot_mod, 'release_url', '') or '')
        data['download_url'] = str(getattr(wxbot_mod, 'download_url', '') or '')
        return jsonify({'status': 'success', 'data': data})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/get_status')
@login_required
def get_status():
    global bot, bot_thread
    cfg = _inject_account_scoped_task_config(read_config() or {})
    if bot_thread and bot_thread.is_alive() and bot:
        try:
            status = bot.get_status()
            runtime_metrics_payload = _dashboard_runtime_metrics_payload(days=1, runtime_bot=bot)
            status['bot_running'] = True
            status['bot_stopping'] = bool(bot_stop_requested.is_set() or getattr(bot, 'is_stop_requested', lambda: False)())
            status = _enrich_dashboard_status_snapshot(
                status,
                cfg=cfg,
                wx_id=str(getattr(bot, 'wx_id', '') or '').strip(),
                runtime_material_ids=set(getattr(bot, '_material_runtime_messages', {}) or {}),
                runtime_metrics_payload=runtime_metrics_payload,
            )
            return jsonify({'status': 'success', 'data': status})
        except Exception as e:
            return jsonify({'status': 'success', 'data': {'bot_running': True, 'bot_stopping': bot_stop_requested.is_set(), 'error': str(e)}})
    elif bot_thread and bot_thread.is_alive():
        return jsonify({'status': 'success', 'data': {'bot_running': True, 'bot_stopping': bot_stop_requested.is_set()}})
    else:
        status = _dashboard_config_status_snapshot(cfg)
        runtime_metrics_payload = _dashboard_runtime_metrics_payload(days=1)
        status['bot_running'] = False
        status['bot_stopping'] = False
        status = _enrich_dashboard_status_snapshot(
            status,
            cfg=cfg,
            wx_id=str(_contact_profiles_picker_options().get('wx_id', '') or '').strip(),
            runtime_material_ids=set(),
            runtime_metrics_payload=runtime_metrics_payload,
        )
        return jsonify({'status': 'success', 'data': status})


@app.route('/api/runtime-metrics')
@login_required
def api_runtime_metrics():
    global bot, bot_thread
    try:
        days = int(request.args.get('days', 7) or 7)
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(365, days))
    try:
        payload = _dashboard_runtime_metrics_payload(
            days=days,
            runtime_bot=bot if bot_thread and bot_thread.is_alive() and bot else None,
        )
        if isinstance(payload, dict):
            return jsonify(payload)
    except Exception:
        pass
    return jsonify({'status': 'success', 'updated_at': '', 'range_days': days, 'hourly': [], 'daily': [], 'today': {}})


@app.route('/api/siver-panel/status')
@login_required
def get_siver_panel_status():
    if siver_panel_manager is None:
        return jsonify({'status': 'error', 'message': 'SiverPanel 客户端未初始化'})
    return jsonify({'status': 'success', 'data': siver_panel_manager.get_status()})


@app.route('/api/siver-panel/connect', methods=['POST'])
@login_required
def connect_siver_panel():
    if siver_panel_manager is None:
        return jsonify({'status': 'error', 'message': 'SiverPanel 客户端未初始化'})
    try:
        status = siver_panel_manager.connect(manual=True)
        if status.get('state') == 'error' and status.get('last_error_code') == 'default_admin_credentials_block_remote_connect':
            return jsonify({
                'status': 'error',
                'message': status.get('last_message') or '远程连接已被安全策略拦截',
                'error_code': status.get('last_error_code') or 'default_admin_credentials_block_remote_connect',
                'data': status,
            })
        return jsonify({'status': 'success', 'message': status.get('last_message') or '正在发起远程连接', 'data': status})
    except Exception as e:
        log('ERROR', f'SiverPanel 手动连接失败: {e}')
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/api/siver-panel/disconnect', methods=['POST'])
@login_required
def disconnect_siver_panel():
    if siver_panel_manager is None:
        return jsonify({'status': 'error', 'message': 'SiverPanel 客户端未初始化'})
    try:
        status = siver_panel_manager.disconnect(reason='manual_disconnect')
        return jsonify({'status': 'success', 'message': status.get('last_message') or '远程访问服务已断开', 'data': status})
    except Exception as e:
        log('ERROR', f'SiverPanel 断开连接失败: {e}')
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/load_config')
@login_required
def load_config():
    config = read_config()
    if not config:
        return jsonify({'status': 'error', 'message': '无法读取配置文件'})
    return jsonify({'status': 'success', 'config': config})

@app.route('/get_admin_config')
@login_required
def get_admin_config():
    try:
        with open(ADMIN_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return jsonify({
            'status': 'success',
            'username': data.get('username', ''),
            'force_admin_change_required': is_force_admin_change_required(),
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/save_admin_config', methods=['POST'])
@login_required
def save_admin_config():
    global USERS
    try:
        was_force_required = is_force_admin_change_required()
        data = request.get_json()
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()
        if not username or not password:
            return jsonify({'status': 'error', 'message': '用户名和密码不能为空'})
        if is_force_admin_change_required() and username == DEFAULT_ADMIN_USERNAME and password == DEFAULT_ADMIN_PASSWORD:
            return jsonify({'status': 'error', 'message': '远程访问时不能继续使用默认账号密码，请修改后再保存'})
        new_creds = {'username': username, 'password_hash': hash_password(password)}
        with open(ADMIN_FILE, 'w', encoding='utf-8') as f:
            json.dump(new_creds, f, ensure_ascii=False, indent=4)
        USERS = new_creds
        session['username'] = username
        log('SUCCESS', f'后台账号已更新，用户名：{username}')
        message = '账号密码已保存，下次登录生效'
        force_admin_change_required = is_force_admin_change_required()
        if was_force_required and not force_admin_change_required:
            message = '账号密码已保存，当前会话限制已解除'
        return jsonify({
            'status': 'success',
            'message': message,
            'force_admin_change_required': force_admin_change_required,
            'username': username,
        })
    except Exception as e:
        log('ERROR', f'保存账号密码失败: {e}')
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/get_email_config')
@login_required
def get_email_config():
    try:
        config = load_email_config(EMAIL_FILE)
        return jsonify({
            'status': 'success',
            **config,
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/save_email_config', methods=['POST'])
@login_required
def save_email_config():
    try:
        data = request.get_json() or {}
        host = data.get('host', '').strip()
        port = data.get('port', '').strip()
        user = data.get('user', '').strip()
        pwd  = data.get('pass', '').strip()
        if not all([host, port, user, pwd]):
            return jsonify({'status': 'error', 'message': '所有字段均不能为空'})
        config = save_email_config_file(
            {'host': host, 'port': port, 'user': user, 'pass': pwd},
            EMAIL_FILE,
        )
        log('SUCCESS', f"邮件配置已更新，SMTP: {config['host']}:{config['port']}，账号: {config['user']}")
        return jsonify({'status': 'success', 'message': '邮件配置已保存'})
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': '端口必须是数字'})
    except Exception as e:
        log('ERROR', f'保存邮件配置失败: {e}')
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/get_webhook_config')
@login_required
def get_webhook_config():
    try:
        config = webhook_send.load_config(WEBHOOK_FILE)
        return jsonify({'status': 'success', **config})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/save_webhook_config', methods=['POST'])
@login_required
def save_webhook_config():
    try:
        data = request.get_json() or {}
        config = webhook_send.save_config(data, WEBHOOK_FILE)
        log('SUCCESS', f"WebHook 配置已更新，启用状态: {config.get('enabled')}")
        return jsonify({'status': 'success', 'message': 'WebHook 配置已保存', 'config': config})
    except Exception as e:
        log('ERROR', f'保存 WebHook 配置失败: {e}')
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/test_webhook', methods=['POST'])
@login_required
def test_webhook():
    try:
        data = request.get_json() or {}
        ok, message = webhook_send.send_webhook('WXBot Pro 测试通知', '这是一条 WebHook 测试消息。', data)
        return jsonify({'status': 'success' if ok else 'error', 'message': message})
    except Exception as e:
        log('ERROR', f'测试 WebHook 失败: {e}')
        return jsonify({'status': 'error', 'message': str(e)})

import threading

_tk_lock = threading.Lock()  # 确保同一时刻只弹一个文件选择框


def _pick_local_file_paths(*, title, filetypes, multiple=False):
    import os
    import tkinter as tk
    from tkinter import filedialog

    with _tk_lock:
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        root.lift()
        try:
            if multiple:
                paths = filedialog.askopenfilenames(
                    parent=root,
                    title=title,
                    filetypes=filetypes,
                )
                normalized = []
                for path in paths or ():
                    path = str(path or '').strip()
                    if path:
                        normalized.append(os.path.normpath(path))
                return normalized
            path = filedialog.askopenfilename(
                parent=root,
                title=title,
                filetypes=filetypes,
            )
            path = str(path or '').strip()
            return [os.path.normpath(path)] if path else []
        finally:
            root.destroy()

@app.route('/pick_image_file', methods=['GET'])
@login_required
def pick_image_file():
    """
    打开 Windows 原生文件选择对话框，让用户选择一张本地图片，
    返回其绝对路径。前端直接将路径填入输入框，无需上传文件。
    """
    try:
        paths = _pick_local_file_paths(
            title='选择图片文件',
            filetypes=[
                ('图片文件', '*.png *.jpg *.jpeg *.gif *.bmp *.webp *.PNG *.JPG *.JPEG'),
                ('所有文件', '*.*'),
            ],
        )
        if paths:
            return jsonify({'status': 'success', 'path': paths[0]})
        return jsonify({'status': 'cancel'})
    except Exception as e:
        log('ERROR', f'文件选择框出错: {e}')
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/pick_image_files', methods=['GET'])
@login_required
def pick_image_files():
    """打开 Windows 原生文件选择对话框，让用户一次选择多张本地图片。"""
    try:
        paths = _pick_local_file_paths(
            title='选择图片文件',
            filetypes=[
                ('图片文件', '*.png *.jpg *.jpeg *.gif *.bmp *.webp *.PNG *.JPG *.JPEG'),
                ('所有文件', '*.*'),
            ],
            multiple=True,
        )
        if paths:
            return jsonify({'status': 'success', 'paths': paths})
        return jsonify({'status': 'cancel'})
    except Exception as e:
        log('ERROR', f'图片多选文件选择框出错: {e}')
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/pick_keyword_reply_file', methods=['GET'])
@login_required
def pick_keyword_reply_file():
    """打开本地文件选择框，返回关键词回复文件的绝对路径。"""
    try:
        paths = _pick_local_file_paths(
            title='选择关键词回复文件',
            filetypes=[
                ('所有文件', '*.*'),
            ],
        )
        if paths:
            return jsonify({'status': 'success', 'path': paths[0]})
        return jsonify({'status': 'cancel'})
    except Exception as e:
        log('ERROR', f'关键词回复文件选择框出错: {e}')
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/pick_keyword_reply_files', methods=['GET'])
@login_required
def pick_keyword_reply_files():
    """打开本地文件选择框，返回关键词回复文件的绝对路径列表。"""
    try:
        paths = _pick_local_file_paths(
            title='选择关键词回复文件',
            filetypes=[
                ('所有文件', '*.*'),
            ],
            multiple=True,
        )
        if paths:
            return jsonify({'status': 'success', 'paths': paths})
        return jsonify({'status': 'cancel'})
    except Exception as e:
        log('ERROR', f'关键词回复多选文件选择框出错: {e}')
        return jsonify({'status': 'error', 'message': str(e)})



def _chat_memory_wx_id_from_request():
    wx_id = str(request.args.get('wx_id', '') or '').strip()
    if wx_id:
        return _validate_known_account_wx_id(wx_id, base_dir=CHAT_MEMORY_BASE)
    try:
        data = request.get_json(silent=True) or {}
        wx_id = str(data.get('wx_id', '') or '').strip()
        if wx_id:
            return _validate_known_account_wx_id(wx_id, base_dir=CHAT_MEMORY_BASE)
    except Exception:
        pass
    preferred = _preferred_account_wx_id(CHAT_MEMORY_BASE)
    if preferred:
        return preferred
    wx_ids = _chat_memory_wx_ids()
    return wx_ids[0] if wx_ids else ''


def _chat_memory_store(wx_id=None):
    wx_id = str(wx_id or '').strip()
    if not wx_id:
        raise ValueError('wx_id is required')
    return ChatMemoryStore(_account_chat_memory_dir(wx_id, create=True))


def _chat_memory_state_has_content(state):
    state = state if isinstance(state, dict) else {}
    memories = state.get('memories') or []
    return bool(memories)


def _chat_memory_merge_incoming_memories(store, chat_name, wx_id, incoming_memories):
    existing_state = store.load_state(chat_name, wx_id=wx_id, strict=True)
    existing_by_id = {
        str(item.get('id', '') or '').strip(): item
        for item in (existing_state.get('memories') or [])
        if isinstance(item, dict) and str(item.get('id', '') or '').strip()
    }
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    merged_memories = []
    for raw_item in incoming_memories if isinstance(incoming_memories, list) else []:
        item = dict(raw_item) if isinstance(raw_item, dict) else {}
        item_id = str(item.get('id', '') or '').strip()
        existing_item = existing_by_id.get(item_id)
        if existing_item:
            item['created_at'] = str(existing_item.get('created_at', '') or '').strip()
            changed = any(
                str(item.get(field, '') or '').strip() != str(existing_item.get(field, '') or '').strip()
                for field in ('importance', 'type', 'content')
            )
            item['updated_at'] = now if changed else str(existing_item.get('updated_at', '') or '').strip()
        else:
            item['created_at'] = now
            item['updated_at'] = now
        merged_memories.append(item)
    return merged_memories


def _chat_memory_messages_for_user(wx_id, chat_name):
    storage_name = resolve_memory_storage_name(chat_name)
    memory_base = _account_memory_dir(wx_id)
    memory_path = os.path.join(memory_base, storage_name, f'{storage_name}_memory.json')
    try:
        if not os.path.exists(memory_path):
            chat_dir = os.path.join(memory_base, storage_name)
            if not os.path.isdir(chat_dir):
                return []
            mem_files = [f for f in os.listdir(chat_dir) if f.endswith('_memory.json')]
            if not mem_files:
                return []
            memory_path = os.path.join(chat_dir, mem_files[0])
        with open(memory_path, 'r', encoding='utf-8') as f:
            messages = json.load(f)
        return messages if isinstance(messages, list) else []
    except Exception:
        return []


def _memory_chat_dir_has_messages(chat_path):
    try:
        mem_files = [f for f in os.listdir(chat_path) if f.endswith('_memory.json')]
    except OSError:
        return False
    for filename in mem_files:
        path = os.path.join(chat_path, filename)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                messages = json.load(f)
            if isinstance(messages, list) and len(messages) > 0:
                return True
        except Exception:
            continue
    return False


def _chat_memory_wx_ids():
    return _available_account_wx_ids(CHAT_MEMORY_BASE)


def _explicit_or_single_chat_memory_wx_id(explicit_wx_id=''):
    wx_id = str(explicit_wx_id or '').strip()
    if wx_id:
        return _validate_known_account_wx_id(wx_id, base_dir=CHAT_MEMORY_BASE), ''
    wx_ids = _chat_memory_wx_ids()
    if len(wx_ids) == 1:
        return wx_ids[0], ''
    if len(wx_ids) > 1:
        return '', '检测到多个微信号，请先选择微信号后再预览 Prompt。'
    return _preferred_account_wx_id(CHAT_MEMORY_BASE), ''


def _contact_profiles_wx_ids():
    return discover_populated_account_ids(CONTACT_PROFILES_DIR)


def _active_contact_profiles_wx_id():
    return _running_wx_id()


def _latest_contact_profiles_wx_id(wx_ids=None):
    wx_ids = list(wx_ids if wx_ids is not None else _contact_profiles_wx_ids())
    latest_wx_id = ''
    latest_updated_at = ''
    for wx_id in wx_ids:
        directory = _load_contact_profiles_directory(wx_id)
        updated_at = str(directory.get('updated_at', '') or '').strip()
        if not latest_wx_id or updated_at > latest_updated_at:
            latest_wx_id = wx_id
            latest_updated_at = updated_at
    return latest_wx_id or (wx_ids[0] if wx_ids else '')


def _contact_profiles_wx_id_from_request():
    wx_id = str(request.args.get('wx_id', '') or '').strip()
    if wx_id:
        return _validate_known_account_wx_id(wx_id, base_dir=CONTACT_PROFILES_DIR)
    preferred = _preferred_account_wx_id(CONTACT_PROFILES_DIR)
    if preferred:
        return preferred
    wx_ids = _contact_profiles_wx_ids()
    return _latest_contact_profiles_wx_id(wx_ids) if wx_ids else DEFAULT_ACCOUNT_ID


def _load_contact_profiles_directory(wx_id):
    wx_id = str(wx_id or '').strip()
    path = contact_directory_path(CONTACT_PROFILES_DIR, wx_id)
    return load_contact_directory(path, wx_id=wx_id) if wx_id else default_contact_directory('')


def _contact_profiles_continue_start_name(directory):
    subjects = directory.get('subjects') if isinstance(directory, dict) else []
    for subject in reversed(subjects or []):
        if not isinstance(subject, dict):
            continue
        if subject.get('subject_type', 'friend') != 'friend':
            continue
        if subject.get('status', 'active') != 'active':
            continue
        for key in ('send_name', 'remark', 'display_name', 'nickname', 'wechat_id'):
            value = str(subject.get(key, '') or '').strip()
            if value:
                return value
    return ''


def _contact_profiles_summary(directory):
    directory = mark_send_name_conflicts(directory)
    subjects = [item for item in directory.get('subjects', []) if isinstance(item, dict)]
    active_subjects = [item for item in subjects if item.get('status', 'active') == 'active']
    tag_counts = defaultdict(int)
    warning_counts = defaultdict(int)
    duplicate_count = 0
    for contact in subjects:
        for tag in contact.get('tags', []) or []:
            tag = str(tag or '').strip()
            if tag:
                tag_counts[tag] += 1
        warnings = list(contact.get('warnings', []) or [])
        if 'duplicate_send_name' in warnings:
            duplicate_count += 1
        for warning in warnings:
            warning_counts[str(warning)] += 1
    return {
        'total': len(subjects),
        'active': len(active_subjects),
        'missing': sum(1 for item in subjects if item.get('status') == 'missing'),
        'tags': len(tag_counts),
        'tag_counts': dict(sorted(tag_counts.items())),
        'warnings': dict(sorted(warning_counts.items())),
        'duplicate_send_names': duplicate_count,
        'repair_candidates': len(contact_repair_candidates(directory)),
        'updated_at': directory.get('updated_at', ''),
        'maintenance_status': (directory.get('maintenance') or {}).get('status', ''),
        'continue_start_name': _contact_profiles_continue_start_name(directory),
    }


def _contact_profiles_tags(directory):
    summary = _contact_profiles_summary(directory)
    return [
        {'tag': tag, 'count': count}
        for tag, count in sorted(summary['tag_counts'].items(), key=lambda item: (-item[1], item[0]))
    ]


def _contact_profiles_browser_contacts(directory):
    contacts = []
    for subject in directory.get('subjects', []) or []:
        if not isinstance(subject, dict):
            continue
        if subject.get('subject_type', 'friend') != 'friend':
            continue
        if subject.get('status', 'active') != 'active':
            continue
        contacts.append({
            'contact_key': str(subject.get('contact_key', '') or ''),
            'nickname': str(
                subject.get('nickname')
                or subject.get('display_name')
                or subject.get('send_name')
                or ''
            ),
            'remark': str(subject.get('remark', '') or ''),
            'wechat_id': str(subject.get('wechat_id', '') or ''),
            'tags': list(subject.get('tags', []) or []),
        })
    contacts.sort(key=lambda item: (
        _wechat_name_sort_key(item.get('nickname') or item.get('wechat_id')),
        str(item.get('contact_key') or ''),
    ))
    return contacts


def _manual_identity_calibration_candidates(wx_id):
    wx_id = str(wx_id or '').strip()
    names = {}

    def add_name(value, source):
        name = str(value or '').strip()
        if not name:
            return
        item = names.setdefault(name, {'name': name, 'sources': []})
        if source and source not in item['sources']:
            item['sources'].append(source)

    directory = _load_contact_profiles_directory(wx_id)
    for subject in directory.get('subjects', []) or []:
        if not isinstance(subject, dict) or subject.get('subject_type', 'friend') != 'friend':
            continue
        if subject.get('status', 'active') != 'active':
            continue
        for key in ('remark', 'nickname', 'display_name', 'send_name', 'wechat_id'):
            add_name(subject.get(key), '通讯录')

    index = _identity_index_for_wx_id(wx_id)
    for identity in index.get('identities') or []:
        if not isinstance(identity, dict):
            continue
        for key in ('current_chat_name', 'remark', 'nickname', 'display_name', 'send_name', 'wechat_id'):
            add_name(identity.get(key), '身份索引')

    for name in list_memory_chat_names(DATA_DIR, wx_id):
        add_name(name, '聊天记录')
    for name in list_chat_memory_names(DATA_DIR, wx_id):
        add_name(name, '会话记忆')

    return sorted(names.values(), key=lambda item: _wechat_name_sort_key(item.get('name')))


def _manual_identity_calibration_wx_ids():
    wx_ids = set(_contact_profiles_wx_ids())
    wx_ids.update(discover_populated_account_ids(MEMORY_BASE))
    wx_ids.update(discover_populated_account_ids(CHAT_MEMORY_BASE))
    wx_ids.update(discover_populated_account_ids(DATA_DIR))
    return sorted(wx_ids)


def _validate_manual_identity_calibration_wx_id(candidate):
    candidate = str(candidate or '').strip()
    wx_ids = _manual_identity_calibration_wx_ids()
    if candidate:
        if candidate in wx_ids or is_known_account_id(
            candidate,
            running_wx_id=_running_wx_id(),
            last_wx_id=_read_last_wx_id(),
            existing_ids=wx_ids,
        ):
            return candidate
        raise ValueError('所选微信号不存在或已失效，请重新选择')
    preferred = _preferred_account_wx_id(DATA_DIR)
    if preferred:
        return preferred
    return wx_ids[0] if wx_ids else DEFAULT_ACCOUNT_ID


def _manual_identity_calibration_wx_id_from_request():
    if request.method == 'GET':
        candidate = request.args.get('wx_id', '')
    elif request.is_json:
        payload = request.get_json(silent=True) or {}
        candidate = payload.get('wx_id', '')
    else:
        candidate = request.form.get('wx_id', '')
    return _validate_manual_identity_calibration_wx_id(candidate)


def _refresh_runtime_identity_after_manual_calibration():
    if bot_thread and bot_thread.is_alive() and bot:
        load_identity_cache = getattr(bot, '_load_identity_index_cache', None)
        if callable(load_identity_cache):
            load_identity_cache()
        refresh_config = getattr(getattr(bot, 'config', None), 'refresh_config', None)
        if callable(refresh_config):
            refresh_config()
        reply_count_store = getattr(bot, 'reply_count_store', None)
        reload_store = getattr(reply_count_store, '_load', None)
        if callable(reload_store):
            reply_count_store.data = reload_store()


def _contact_profiles_browser_tag_items(directory):
    contacts = _contact_profiles_browser_contacts(directory)
    tag_counts = defaultdict(int)
    for contact in contacts:
        for tag in contact.get('tags', []) or []:
            tag = str(tag or '').strip()
            if tag:
                tag_counts[tag] += 1
    return [{
        'tag': '__all__',
        'label': '全部联系人',
        'count': len(contacts),
    }] + [
        {'tag': tag, 'label': tag, 'count': count}
        for tag, count in sorted(tag_counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _contact_profiles_browser_payload(wx_id=''):
    picker = _contact_profiles_picker_options(wx_id)
    wx_ids = picker.get('wx_ids', [])
    selected_wx_id = str(picker.get('wx_id', '') or '').strip()
    directory = _load_contact_profiles_directory(selected_wx_id) if selected_wx_id else default_contact_directory('')
    return {
        'wx_ids': wx_ids,
        'wx_id': selected_wx_id,
        'tag_items': _contact_profiles_browser_tag_items(directory),
        'contacts': _contact_profiles_browser_contacts(directory),
        'summary': _contact_profiles_summary(directory),
    }


def _contact_profiles_delete_wx_namespace(wx_id):
    wx_id = str(wx_id or '').strip()
    if not wx_id:
        raise ValueError('请先选择微信号')
    target = os.path.abspath(contact_directory_path(CONTACT_PROFILES_DIR, wx_id).parent)
    base = os.path.abspath(str(account_dir(CONTACT_PROFILES_DIR, wx_id).parent))
    if os.path.commonpath([base, target]) != base:
        raise ValueError('微信号路径无效')
    if os.path.isdir(target):
        shutil.rmtree(target)


def _contact_profiles_remove_tag(wx_id, tag):
    wx_id = str(wx_id or '').strip()
    tag = unquote(str(tag or '').strip())
    if not wx_id:
        raise ValueError('请先选择微信号')
    if not tag or tag == '__all__':
        raise ValueError('该标签不允许删除')
    path = contact_directory_path(CONTACT_PROFILES_DIR, wx_id)
    directory = load_contact_directory(path, wx_id=wx_id)
    changed = False
    for subject in directory.get('subjects') or []:
        if not isinstance(subject, dict):
            continue
        old_tags = normalize_tag_list(subject.get('tags'))
        new_tags = [item for item in old_tags if item != tag]
        if new_tags == old_tags:
            continue
        subject['tags'] = new_tags
        raw_tags = subject.get('raw_tags')
        if isinstance(raw_tags, (list, tuple, set)):
            subject['raw_tags'] = new_tags
        else:
            subject['raw_tags'] = '，'.join(new_tags)
        changed = True
    if changed:
        directory['updated_at'] = datetime.now().replace(microsecond=0).isoformat()
        save_contact_directory(path, directory)
    return changed


def _contact_profiles_picker_options(wx_id=''):
    wx_ids = list(_contact_profiles_wx_ids())
    selected_wx_id = str(wx_id or '').strip()
    if selected_wx_id:
        try:
            selected_wx_id = _validate_known_account_wx_id(selected_wx_id, base_dir=CONTACT_PROFILES_DIR)
        except ValueError:
            selected_wx_id = ''
    if not selected_wx_id:
        selected_wx_id = _preferred_account_wx_id(CONTACT_PROFILES_DIR)
    if not selected_wx_id and wx_ids:
        selected_wx_id = _latest_contact_profiles_wx_id(wx_ids)
    if selected_wx_id and selected_wx_id not in wx_ids:
        wx_ids.append(selected_wx_id)
    directory = _load_contact_profiles_directory(selected_wx_id) if selected_wx_id else default_contact_directory('')
    directory = mark_send_name_conflicts(directory)
    contacts = []
    for subject in directory.get('subjects', []) or []:
        if not isinstance(subject, dict):
            continue
        if subject.get('subject_type', 'friend') != 'friend':
            continue
        if subject.get('status', 'active') != 'active':
            continue
        contacts.append({
            'contact_key': str(subject.get('contact_key', '') or ''),
            'display_name': str(subject.get('display_name', '') or subject.get('send_name', '') or ''),
            'send_name': str(subject.get('send_name', '') or ''),
            'tags': list(subject.get('tags', []) or []),
            'warnings': list(subject.get('warnings', []) or []),
        })
    return {
        'wx_ids': wx_ids,
        'wx_id': selected_wx_id,
        'summary': _contact_profiles_summary(directory),
        'tags': _contact_profiles_tags(directory),
        'contacts': contacts,
    }


def _relationship_scan_wx_id_from_request():
    wx_id = _contact_profiles_runtime_wx_id_from_request()
    if wx_id:
        return _validate_known_account_wx_id(wx_id, base_dir=CONTACT_PROFILES_DIR)
    return _contact_profiles_wx_id_from_request()


def _relationship_scan_payload(wx_id=''):
    wx_id = str(wx_id or '').strip()
    if not wx_id:
        wx_id = _contact_profiles_wx_id_from_request()
    state = relationship_scan.load_state(DATA_DIR, wx_id)
    runtime = state.setdefault('runtime', {})
    if (
        runtime.get('full_scan_running')
        and not (relationship_full_scan_thread and relationship_full_scan_thread.is_alive())
    ):
        runtime['full_scan_running'] = False
        runtime['stop_requested'] = False
        progress = runtime.get('full_scan_progress') if isinstance(runtime.get('full_scan_progress'), dict) else {}
        progress.update({
            'status': 'failed',
            'updated_at': datetime.now().replace(microsecond=0).isoformat(),
            'message': '全量扫描已中断，请重新开始',
        })
        runtime['full_scan_progress'] = progress
        state = relationship_scan.save_state(DATA_DIR, state)
    payload = relationship_scan.relationship_scan_payload(state)
    wx_ids = _available_account_wx_ids(CONTACT_PROFILES_DIR)
    if wx_id and wx_id not in wx_ids:
        wx_ids.append(wx_id)
    payload['wx_ids'] = wx_ids
    return payload


def _save_relationship_scan_settings(wx_id, raw_settings):
    wx_id = str(wx_id or '').strip()
    state = relationship_scan.load_state(DATA_DIR, wx_id)
    state['settings'] = relationship_scan.normalize_settings({
        **(state.get('settings') or {}),
        **(raw_settings or {}),
    })
    state = relationship_scan.save_state(DATA_DIR, state)
    return relationship_scan.relationship_scan_payload(state)


def _friend_request_wx_id_from_request():
    wx_id = _contact_profiles_runtime_wx_id_from_request()
    if wx_id:
        return _validate_known_account_wx_id(wx_id, base_dir=CONTACT_PROFILES_DIR)
    return _contact_profiles_wx_id_from_request()


def _friend_request_payload(wx_id=''):
    wx_id = str(wx_id or '').strip()
    if not wx_id:
        wx_id = _contact_profiles_wx_id_from_request()
    state = friend_request.load_state(DATA_DIR, wx_id)
    payload = friend_request.friend_request_payload(state)
    wx_ids = _available_account_wx_ids(CONTACT_PROFILES_DIR)
    if wx_id and wx_id not in wx_ids:
        wx_ids.append(wx_id)
    payload['wx_id'] = wx_id
    payload['wx_ids'] = wx_ids
    return payload


def _save_friend_request_settings(wx_id, raw_data):
    wx_id = str(wx_id or '').strip()
    state = friend_request.load_state(DATA_DIR, wx_id)
    raw_data = raw_data or {}
    old_settings = friend_request.normalize_settings(state.get('settings'))
    state['settings'] = friend_request.normalize_settings({
        **(state.get('settings') or {}),
        **(raw_data.get('settings') or raw_data),
    })
    new_settings = state['settings']
    if 'message_rules' in raw_data:
        rules = [friend_request.normalize_message_rule(item) for item in (raw_data.get('message_rules') or [])]
        state['message_rules'] = [item for item in rules if item]
    state = friend_request.save_state(DATA_DIR, state)
    if (
        old_settings.get('add_object') != new_settings.get('add_object')
        or old_settings.get('include_tags') != new_settings.get('include_tags')
    ):
        state = friend_request.refresh_candidates(DATA_DIR, state.get('wx_id') or wx_id)
    return _friend_request_payload(state.get('wx_id') or wx_id)


def _contact_profiles_runtime_wx_id_from_request():
    candidate = str(request.args.get('wx_id', '') or '').strip()
    if candidate:
        return candidate
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        return str(payload.get('wx_id', '') or '').strip()
    if request.method == 'POST':
        return str(request.form.get('wx_id', '') or '').strip()
    return ''


def _require_running_contact_profiles_wx_id():
    if not bot_thread or not bot_thread.is_alive() or not bot:
        raise ValueError('请先启动机器人，并保持微信主窗口可用。')
    running_wx_id = str(getattr(bot, 'wx_id', '') or '').strip()
    requested_wx_id = _contact_profiles_runtime_wx_id_from_request()
    if requested_wx_id and running_wx_id and requested_wx_id != running_wx_id:
        raise ValueError(f'当前运行账号是 {running_wx_id}，请先切回该微信号后再操作。')
    return running_wx_id or requested_wx_id


@app.route('/contact_profiles/wx_ids')
@login_required
def contact_profiles_wx_ids():
    """返回已有通讯录档案的微信号目录。"""
    try:
        return jsonify({'status': 'success', 'wx_ids': _contact_profiles_wx_ids()})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/contact_profiles/directory')
@login_required
def contact_profiles_directory():
    """读取当前微信号的通讯录档案；不存在时返回空目录。"""
    try:
        wx_id = _contact_profiles_wx_id_from_request()
        directory = _load_contact_profiles_directory(wx_id)
        directory = mark_send_name_conflicts(directory)
        return jsonify({
            'status': 'success',
            'wx_id': wx_id,
            'directory': directory,
            'summary': _contact_profiles_summary(directory),
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/contact_profiles/tags')
@login_required
def contact_profiles_tags():
    """返回当前微信号通讯录标签统计。"""
    try:
        wx_id = _contact_profiles_wx_id_from_request()
        directory = _load_contact_profiles_directory(wx_id)
        return jsonify({
            'status': 'success',
            'wx_id': wx_id,
            'tags': _contact_profiles_tags(directory),
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/contact_profiles/browser')
@login_required
def contact_profiles_browser():
    """返回通讯录三栏查看器所需的微信号、标签和联系人列表。"""
    try:
        wx_id = _contact_profiles_wx_id_from_request()
        return jsonify({'status': 'success', **_contact_profiles_browser_payload(wx_id)})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/contact_profiles/identity_calibration')
@login_required
def contact_profiles_identity_calibration():
    """返回身份校准待确认项。"""
    try:
        wx_id = _contact_profiles_wx_id_from_request()
        index = _identity_index_for_wx_id(wx_id)
        pending = [
            item for item in (index.get('pending') or [])
            if isinstance(item, dict) and str(item.get('status') or 'pending') == 'pending'
        ]
        return jsonify({
            'status': 'success',
            'wx_id': wx_id,
            'pending': pending,
            'count': len(pending),
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/contact_profiles/manual_identity_calibration/candidates')
@login_required
def contact_profiles_manual_identity_calibration_candidates():
    """返回手动校准旧名字候选。"""
    try:
        wx_id = _manual_identity_calibration_wx_id_from_request()
        candidates = _manual_identity_calibration_candidates(wx_id)
        return jsonify({
            'status': 'success',
            'wx_id': wx_id,
            'candidates': candidates,
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/contact_profiles/manual_identity_calibration', methods=['POST'])
@login_required
def contact_profiles_manual_identity_calibration():
    """手动确认旧名字和新备注/昵称是同一人，并立即合并本地资料。"""
    try:
        data = request.get_json(silent=True) or {}
        wx_id = str(data.get('wx_id') or '').strip()
        if wx_id:
            wx_id = _validate_manual_identity_calibration_wx_id(wx_id)
        else:
            wx_id = _manual_identity_calibration_wx_id_from_request()
        old_name = str(data.get('old_name') or '').strip()
        new_name = str(data.get('new_name') or '').strip()
        if not old_name:
            return jsonify({'status': 'error', 'message': '请选择旧名字'}), 400
        if not new_name:
            return jsonify({'status': 'error', 'message': '请输入新备注/昵称'}), 400
        if old_name == new_name:
            return jsonify({'status': 'error', 'message': '旧名字和新备注/昵称不能相同'}), 400

        known_names = {item['name'] for item in _manual_identity_calibration_candidates(wx_id)}
        if old_name not in known_names:
            return jsonify({'status': 'error', 'message': f'未找到旧名字「{old_name}」，请刷新候选后重试'}), 404

        manifest = reconcile_identity_storage_names(
            DATA_DIR,
            wx_id,
            old_name,
            new_name,
            reason='manual_direct_identity_calibration',
        )
        _refresh_runtime_identity_after_manual_calibration()
        log('SUCCESS', f'[身份校准] 手动校准已合并：{old_name} -> {new_name}')
        return jsonify({
            'status': 'success',
            'wx_id': wx_id,
            'message': f'已合并：{old_name} -> {new_name}',
            'manifest': manifest,
            'browser': _contact_profiles_browser_payload(wx_id),
            'candidates': _manual_identity_calibration_candidates(wx_id),
        })
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/contact_profiles/identity_calibration/<fingerprint>/dismiss', methods=['POST'])
@login_required
def contact_profiles_identity_calibration_dismiss(fingerprint):
    """确认两个身份不是同一人，避免反复提示。"""
    try:
        wx_id = _contact_profiles_wx_id_from_request()
        index = dismiss_identity_pending(_identity_index_for_wx_id(wx_id), fingerprint)
        _save_identity_index_for_wx_id(wx_id, index)
        log('INFO', f'[身份校准] 已标记不是同一人：{fingerprint}')
        return jsonify({
            'status': 'success',
            'wx_id': wx_id,
            'message': '已标记为不是同一人',
            'pending': [item for item in (index.get('pending') or []) if item.get('status') == 'pending'],
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/contact_profiles/identity_calibration/<fingerprint>/merge', methods=['POST'])
@login_required
def contact_profiles_identity_calibration_merge(fingerprint):
    """确认两个身份是同一人，并自动合并聊天记录和会话记忆。"""
    try:
        wx_id = _contact_profiles_wx_id_from_request()
        index = _identity_index_for_wx_id(wx_id)
        target = None
        for item in index.get('pending') or []:
            if isinstance(item, dict) and str(item.get('fingerprint') or '') == str(fingerprint or ''):
                target = item
                break
        if not target:
            return jsonify({'status': 'error', 'message': '待确认项不存在'}), 404
        old_snapshot = target.get('old_snapshot') if isinstance(target.get('old_snapshot'), dict) else {}
        new_snapshot = target.get('new_snapshot') if isinstance(target.get('new_snapshot'), dict) else {}
        old_name = str(old_snapshot.get('current_chat_name') or '').strip()
        new_name = str(new_snapshot.get('current_chat_name') or '').strip()
        if not old_name or not new_name:
            return jsonify({'status': 'error', 'message': '待确认项缺少可合并的会话名'}), 400
        manifest = reconcile_identity_storage_names(
            DATA_DIR,
            wx_id,
            old_name,
            new_name,
            reason='manual_identity_calibration',
        )
        new_identity_id = str(target.get('new_identity_id') or '').strip()
        old_identity_id = str(target.get('old_identity_id') or '').strip()
        merged_identities = []
        for identity in index.get('identities') or []:
            identity_id = str(identity.get('identity_id') or '').strip()
            if new_identity_id and identity_id == new_identity_id:
                continue
            if identity_id == old_identity_id:
                identity.update({
                    'current_chat_name': new_name,
                    'storage_name': resolve_memory_storage_name(new_name),
                    'wechat_id': str(new_snapshot.get('wechat_id') or ''),
                    'remark': str(new_snapshot.get('remark') or ''),
                    'nickname': str(new_snapshot.get('nickname') or ''),
                    'display_name': str(new_snapshot.get('display_name') or new_name),
                    'send_name': str(new_snapshot.get('send_name') or new_name),
                    'region': str(new_snapshot.get('region') or ''),
                    'source': str(new_snapshot.get('source') or ''),
                    'added_at': str(new_snapshot.get('added_at') or ''),
                    'signature': str(new_snapshot.get('signature') or ''),
                    'last_seen_at': str(new_snapshot.get('last_seen_at') or identity.get('last_seen_at') or ''),
                    'updated_at': datetime.now().replace(microsecond=0).isoformat(),
                })
            merged_identities.append(identity)
        index['identities'] = merged_identities
        new_fingerprint_snapshot = json.dumps(new_snapshot, ensure_ascii=False, sort_keys=True)
        index['pending'] = [
            item for item in (index.get('pending') or [])
            if not (
                isinstance(item, dict)
                and (
                    str(item.get('fingerprint') or '') == str(fingerprint or '')
                    or (old_identity_id and str(item.get('old_identity_id') or '') == old_identity_id)
                    or (new_identity_id and str(item.get('new_identity_id') or '') == new_identity_id)
                    or json.dumps(item.get('new_snapshot') if isinstance(item.get('new_snapshot'), dict) else {}, ensure_ascii=False, sort_keys=True) == new_fingerprint_snapshot
                )
            )
        ]
        index = _save_identity_index_for_wx_id(wx_id, index)
        _refresh_runtime_identity_after_manual_calibration()
        log('SUCCESS', f'[身份校准] 已确认同一人并合并：{old_name} -> {new_name}')
        return jsonify({
            'status': 'success',
            'wx_id': wx_id,
            'message': '已合并聊天记录和会话记忆',
            'manifest': manifest,
            'pending': [item for item in (index.get('pending') or []) if item.get('status') == 'pending'],
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/contact_profiles/wx/<wx_id>', methods=['DELETE'])
@login_required
def contact_profiles_wx_delete(wx_id):
    """删除指定微信号下的本地通讯录档案。"""
    try:
        wx_id = str(wx_id or '').strip()
        _contact_profiles_delete_wx_namespace(wx_id)
        return jsonify({
            'status': 'success',
            'wx_id': wx_id,
            'message': '已删除该微信号下的本地通讯录档案',
            'browser': _contact_profiles_browser_payload(''),
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/contact_profiles/tag/<path:tag>', methods=['DELETE'])
@login_required
def contact_profiles_tag_delete(tag):
    """删除指定微信号下的一个本地通讯录标签。"""
    try:
        tag = unquote(str(tag or '').strip())
        wx_id = _contact_profiles_wx_id_from_request()
        if tag == '__all__':
            _contact_profiles_delete_wx_namespace(wx_id)
            return jsonify({
                'status': 'success',
                'wx_id': wx_id,
                'message': '已清空该微信号下的全部联系人建档记录',
                'browser': _contact_profiles_browser_payload(''),
            })
        changed = _contact_profiles_remove_tag(wx_id, tag)
        message = f'已删除标签「{tag}」' if changed else f'标签「{tag}」不存在'
        return jsonify({
            'status': 'success',
            'wx_id': wx_id,
            'message': message,
            'browser': _contact_profiles_browser_payload(wx_id),
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/contact_profiles/refresh_batch', methods=['POST'])
@login_required
def contact_profiles_refresh_batch():
    """执行一次真实 wxauto 通讯录读取批次。"""
    try:
        if not (bot_thread and bot_thread.is_alive() and bot and hasattr(bot, 'refresh_contact_profiles_batch')):
            return jsonify({'status': 'error', 'message': '请先启动机器人，并保持微信主窗口可用。'})
        data = request.get_json(silent=True) or {}
        running_wx_id = _require_running_contact_profiles_wx_id()
        mode = str(data.get('mode', 'standard') or 'standard').strip()
        start_name = str(data.get('start_name', '') or '').strip()
        interval = data.get('interval')
        mode_label = {
            'test': '快速测试',
            'standard': '立即建档',
            'force': '暴力建档',
        }.get(mode, '通讯录维护')
        log('INFO', f'[通讯录维护] 收到{mode_label}请求，起点：{start_name or "通讯录头部"}')
        result = bot.refresh_contact_profiles_batch(
            mode=mode,
            start_name=start_name,
            interval=interval,
            run_to_completion=(mode != 'test'),
        )
        wx_id = str(result.get('wx_id', '') or '').strip()
        total_count = int(result.get("count_returned", 0) or 0)
        read_item_count = int(result.get("read_item_count", total_count) or 0)
        new_unique_count = int(result.get("new_unique_count", 0) or 0)
        directory_total_unique_count = int(result.get("directory_total_unique_count", 0) or 0)
        completed = bool(result.get("completed", False))
        stopped_reason = str(result.get("stopped_reason", "") or "").strip()
        stopped_early = bool(result.get("stopped_early", False))
        summary_message = (
            f"本轮读取 {read_item_count} 条，本轮新增 {new_unique_count} 个唯一联系人，"
            f"当前共 {directory_total_unique_count} 个联系人"
        )
        if stopped_reason == "manual_cap_reached":
            summary_message = "标准建档已到 50 人上限，" + summary_message
        elif stopped_reason == "stalled":
            summary_message = "建档疑似卡住，已停止重试，" + summary_message
        elif completed:
            summary_message = "通讯录已读取完成，" + summary_message
        elif stopped_early:
            summary_message = "已停止建档，" + summary_message
        if stopped_early:
            log('WARNING', f'[通讯录维护] {mode_label}已停止，本次读取 {total_count} 个好友')
        else:
            log('SUCCESS', f'[通讯录维护] {mode_label}完成，本次读取 {total_count} 个好友')
        return jsonify({
            'status': 'success',
            'message': summary_message,
            'data': result,
            'browser': _contact_profiles_browser_payload(wx_id or running_wx_id) if (wx_id or running_wx_id) else _contact_profiles_browser_payload(''),
        })
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        log('ERROR', f'[通讯录维护] 建档失败：{e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/contact_profiles/pause', methods=['POST'])
@login_required
def contact_profiles_pause():
    """暂停或恢复通讯录维护。"""
    try:
        if not (bot_thread and bot_thread.is_alive() and bot and hasattr(bot, 'set_contact_profiles_paused')):
            return jsonify({'status': 'error', 'message': '请先启动机器人，再设置通讯录维护状态。'})
        data = request.get_json(silent=True) or {}
        running_wx_id = _require_running_contact_profiles_wx_id()
        paused = bool(data.get('paused', True))
        directory = bot.set_contact_profiles_paused(paused)
        wx_id = str(directory.get('wx_id', '') or running_wx_id or getattr(bot, 'wx_id', '') or '').strip()
        return jsonify({
            'status': 'success',
            'message': '已请求停止建档，会尽快停止；若当前读取未被打断，则会在本批返回后停止' if paused else '已恢复建档状态',
            'data': directory,
            'browser': _contact_profiles_browser_payload(wx_id) if wx_id else _contact_profiles_browser_payload(''),
        })
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/contact_profiles/repair_remarks', methods=['POST'])
@login_required
def contact_profiles_repair_remarks():
    """执行当前微信号通讯录备注修复。"""
    try:
        if not (bot_thread and bot_thread.is_alive() and bot and hasattr(bot, 'repair_contact_profile_remarks')):
            return jsonify({'status': 'error', 'message': '请先启动机器人，并保持微信主窗口可用。'})
        running_wx_id = _require_running_contact_profiles_wx_id()
        result = bot.repair_contact_profile_remarks()
        wx_id = str(result.get('wx_id', '') or running_wx_id or getattr(bot, 'wx_id', '') or '').strip()
        candidate_count = int(result.get('candidate_count', 0) or 0)
        success_count = int(result.get('success_count', 0) or 0)
        failed_count = int(result.get('failed_count', 0) or 0)
        skipped_count = int(result.get('skipped_count', 0) or 0)
        if candidate_count <= 0:
            message = '当前没有可修复联系人。'
        else:
            message = f'备注修复完成：成功 {success_count}，失败 {failed_count}，跳过 {skipped_count}。'
        return jsonify({
            'status': 'success',
            'message': message,
            'data': result,
            'browser': _contact_profiles_browser_payload(wx_id) if wx_id else _contact_profiles_browser_payload(''),
        })
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/contact_profiles/repair_preview')
@login_required
def contact_profiles_repair_preview():
    """返回当前微信号通讯录备注修复预览。"""
    try:
        if not (bot_thread and bot_thread.is_alive() and bot and hasattr(bot, 'preview_contact_profile_remark_repairs')):
            return jsonify({'status': 'error', 'message': '请先启动机器人，并保持微信主窗口可用。'}), 400
        _require_running_contact_profiles_wx_id()
        result = bot.preview_contact_profile_remark_repairs()
        return jsonify({'status': 'success', 'data': result})
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/relationship_scan/status')
@login_required
def relationship_scan_status():
    try:
        wx_id = _relationship_scan_wx_id_from_request()
        return jsonify({'status': 'success', 'payload': _relationship_scan_payload(wx_id)})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/relationship_scan/settings', methods=['POST'])
@login_required
def relationship_scan_settings_save():
    try:
        data = request.get_json(silent=True) or {}
        wx_id = _relationship_scan_wx_id_from_request()
        payload = _save_relationship_scan_settings(wx_id, data.get('settings') or data)
        return jsonify({'status': 'success', 'message': '关系扫描设置已保存', 'payload': payload})
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/relationship_scan/scan', methods=['POST'])
@login_required
def relationship_scan_manual_scan():
    try:
        if not (bot_thread and bot_thread.is_alive() and bot and hasattr(bot, 'scan_relationship_sessions')):
            return jsonify({'status': 'error', 'message': '请先启动机器人，并保持微信主窗口可用。'})
        _require_running_contact_profiles_wx_id()
        result = bot.scan_relationship_sessions()
        count = len(result.get('sessions') or [])
        return jsonify({
            'status': 'success',
            'message': f'关系扫描完成，本轮读取 {count} 个会话',
            'payload': result.get('payload') or {},
        })
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        log('ERROR', f'[关系扫描] 立即扫描失败：{e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500


def _relationship_full_scan_worker():
    global relationship_full_scan_thread
    try:
        if not (bot_thread and bot_thread.is_alive() and bot and hasattr(bot, 'full_scan_relationship_sessions')):
            wx_id = str(getattr(bot, 'wx_id', '') or '').strip() if bot else ''
            state = relationship_scan.load_state(DATA_DIR, wx_id)
            runtime = state.setdefault('runtime', {})
            runtime['full_scan_running'] = False
            runtime['stop_requested'] = False
            runtime['full_scan_progress'] = {
                **(runtime.get('full_scan_progress') if isinstance(runtime.get('full_scan_progress'), dict) else {}),
                'status': 'failed',
                'updated_at': datetime.now().replace(microsecond=0).isoformat(),
                'message': '机器人已停止，全量扫描中断',
            }
            relationship_scan.save_state(DATA_DIR, state)
            return
        result = bot.full_scan_relationship_sessions(allow_running=True)
        if result.get('already_running'):
            log('INFO', '[关系扫描] 全量扫描已在运行，本次后台任务退出')
            return
        count = len(result.get('sessions') or [])
        log('INFO', f'[关系扫描] 后台全量扫描完成，本轮读取 {count} 个会话')
    except Exception as e:
        log('ERROR', f'[关系扫描] 后台全量扫描失败：{e}')
        try:
            wx_id = str(getattr(bot, 'wx_id', '') or '').strip() if bot else ''
            state = relationship_scan.load_state(DATA_DIR, wx_id)
            runtime = state.setdefault('runtime', {})
            runtime['full_scan_running'] = False
            runtime['stop_requested'] = False
            runtime['full_scan_progress'] = {
                **(runtime.get('full_scan_progress') if isinstance(runtime.get('full_scan_progress'), dict) else {}),
                'status': 'failed',
                'updated_at': datetime.now().replace(microsecond=0).isoformat(),
                'message': f'全量扫描失败：{e}',
                'error': str(e),
            }
            relationship_scan.save_state(DATA_DIR, state)
        except Exception as state_exc:
            log('ERROR', f'[关系扫描] 写入后台扫描失败状态失败：{state_exc}')
    finally:
        with relationship_full_scan_thread_lock:
            relationship_full_scan_thread = None


@app.route('/relationship_scan/full_scan', methods=['POST'])
@login_required
def relationship_scan_full_scan():
    try:
        global relationship_full_scan_thread
        if not (bot_thread and bot_thread.is_alive() and bot and hasattr(bot, 'full_scan_relationship_sessions')):
            return jsonify({'status': 'error', 'message': '请先启动机器人，并保持微信主窗口可用。'})
        _require_running_contact_profiles_wx_id()
        log('INFO', '[关系扫描] 收到全量扫描请求')
        wx_id = _relationship_scan_wx_id_from_request()
        state = relationship_scan.load_state(DATA_DIR, wx_id)
        with relationship_full_scan_thread_lock:
            runtime = state.setdefault('runtime', {})
            stale_running = bool(runtime.get('full_scan_running')) and not (
                relationship_full_scan_thread and relationship_full_scan_thread.is_alive()
            )
            if stale_running:
                runtime['full_scan_running'] = False
                runtime['stop_requested'] = False
                progress = runtime.get('full_scan_progress') if isinstance(runtime.get('full_scan_progress'), dict) else {}
                progress.update({
                    'status': 'failed',
                    'updated_at': datetime.now().replace(microsecond=0).isoformat(),
                    'message': '上次全量扫描已中断，已允许重新开始',
                })
                runtime['full_scan_progress'] = progress
                state = relationship_scan.save_state(DATA_DIR, state)
            elif runtime.get('full_scan_running'):
                return jsonify({
                    'status': 'success',
                    'message': '全量扫描正在运行，本次点击已忽略',
                    'payload': relationship_scan.relationship_scan_payload(state),
                })
            if relationship_full_scan_thread and relationship_full_scan_thread.is_alive():
                return jsonify({
                    'status': 'success',
                    'message': '全量扫描正在运行，本次点击已忽略',
                    'payload': relationship_scan.relationship_scan_payload(state),
                })
            runtime = state.setdefault('runtime', {})
            runtime['full_scan_running'] = True
            runtime['stop_requested'] = False
            runtime['full_scan_progress'] = {
                'status': 'running',
                'started_at': datetime.now().replace(microsecond=0).isoformat(),
                'updated_at': datetime.now().replace(microsecond=0).isoformat(),
                'scrolled_rounds': 0,
                'max_scrolls': relationship_scan.FULL_SCAN_MAX_SCROLLS,
                'unique_count': 0,
                'last_name': '',
                'message': '全量扫描正在启动',
            }
            state = relationship_scan.save_state(DATA_DIR, state)
            relationship_full_scan_thread = threading.Thread(target=_relationship_full_scan_worker, daemon=True)
            relationship_full_scan_thread.start()
        return jsonify({
            'status': 'success',
            'message': '全量扫描已开始，可在面板查看进度',
            'payload': relationship_scan.relationship_scan_payload(state),
        })
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        log('ERROR', f'[关系扫描] 全量扫描失败：{e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/relationship_scan/stop', methods=['POST'])
@login_required
def relationship_scan_stop():
    try:
        if bot_thread and bot_thread.is_alive() and bot and hasattr(bot, 'stop_relationship_full_scan'):
            _require_running_contact_profiles_wx_id()
            wx_id = _relationship_scan_wx_id_from_request()
            state = relationship_scan.load_state(DATA_DIR, wx_id)
            runtime = state.setdefault('runtime', {})
            if not runtime.get('full_scan_running'):
                return jsonify({'status': 'success', 'message': '当前没有正在运行的全量扫描', 'payload': relationship_scan.relationship_scan_payload(state)})
            payload = bot.stop_relationship_full_scan()
        else:
            wx_id = _relationship_scan_wx_id_from_request()
            state = relationship_scan.load_state(DATA_DIR, wx_id)
            if not (state.get('runtime') or {}).get('full_scan_running'):
                return jsonify({'status': 'success', 'message': '当前没有正在运行的全量扫描', 'payload': relationship_scan.relationship_scan_payload(state)})
            state.setdefault('runtime', {})['stop_requested'] = True
            state = relationship_scan.save_state(DATA_DIR, state)
            payload = relationship_scan.relationship_scan_payload(state)
        return jsonify({'status': 'success', 'message': '已请求停止全量扫描', 'payload': payload})
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/relationship_scan/clear', methods=['POST'])
@login_required
def relationship_scan_clear():
    try:
        wx_id = _relationship_scan_wx_id_from_request()
        state = relationship_scan.load_state(DATA_DIR, wx_id)
        if (state.get('runtime') or {}).get('full_scan_running'):
            return jsonify({'status': 'error', 'message': '全量扫描正在运行，请先停止扫描后再清空结果。'})
        state = relationship_scan.save_state(DATA_DIR, relationship_scan.clear_state(state))
        payload = _relationship_scan_payload(state.get('wx_id') or wx_id)
        return jsonify({'status': 'success', 'message': '已清空关系扫描结果和待同步队列，并暂停自动扫描和微信标签同步', 'payload': payload})
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/friend_request/status')
@login_required
def friend_request_status():
    try:
        wx_id = _friend_request_wx_id_from_request()
        return jsonify({'status': 'success', 'payload': _friend_request_payload(wx_id)})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/friend_request/settings', methods=['POST'])
@login_required
def friend_request_settings_save():
    try:
        data = request.get_json(silent=True) or {}
        wx_id = _friend_request_wx_id_from_request()
        payload = _save_friend_request_settings(wx_id, data)
        return jsonify({'status': 'success', 'message': '好友申请设置已保存', 'payload': payload})
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/friend_request/refresh_candidates', methods=['POST'])
@login_required
def friend_request_refresh_candidates():
    try:
        wx_id = _friend_request_wx_id_from_request()
        state = friend_request.refresh_candidates(DATA_DIR, wx_id, contact_base_dir=CONTACT_PROFILES_DIR)
        payload = _friend_request_payload(wx_id)
        return jsonify({
            'status': 'success',
            'message': f"已刷新候选人，共 {len(payload.get('candidates') or [])} 人",
            'payload': payload,
        })
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/friend_request/run_once', methods=['POST'])
@login_required
def friend_request_run_once():
    try:
        if not (bot_thread and bot_thread.is_alive() and bot and hasattr(bot, 'run_friend_request_once')):
            return jsonify({'status': 'error', 'message': '请先启动机器人，并保持微信主窗口可用。'})
        _require_running_contact_profiles_wx_id()
        result = bot.run_friend_request_once(force=True)
        status = result.get('status') or 'failed'
        ok = status in {'sent', 'skipped'}
        wx_id = str(getattr(bot, 'wx_id', '') or '').strip()
        return jsonify({
            'status': 'success' if ok else 'error',
            'message': result.get('message') or ('执行完成' if ok else '执行失败'),
            'payload': _friend_request_payload(wx_id) if wx_id else (result.get('payload') or {}),
            'result': result.get('result') or {},
        })
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        log('ERROR', f'[好友申请] 手动执行失败：{e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/material_outreach/task_records/<task_id>', methods=['DELETE'])
@login_required
def material_outreach_task_records_delete(task_id):
    """删除一个素材转发任务的本地发送记录，不影响任务配置本身。"""
    try:
        wx_id = _current_material_outreach_wx_id()
        removed = _delete_material_outreach_task_records(task_id, wx_id)
        config = _inject_account_scoped_task_config(read_config() or {}, wx_id=wx_id)
        materials = _load_material_outreach_materials(wx_id)
        history = _load_material_outreach_history(wx_id)
        send_records = history['send_records']
        skip_records = history['skip_records']
        progress_records = history['progress_records']
        browser = build_material_outreach_browser(config.get('material_outreach_list', []), progress_records, materials)
        stats = material_outreach_stats(
            materials,
            send_records,
            skip_records,
            runtime_material_ids=set(getattr(bot, '_material_runtime_messages', {}) or {}) if (bot_thread and bot_thread.is_alive() and bot) else set(),
        )
        total_removed = sum(removed.values())
        return jsonify({
            'status': 'success',
            'message': f'已删除该任务的 {total_removed} 条转发记录',
            'removed': removed,
            'browser': browser,
            'stats': stats,
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/material_outreach/materials/<material_id>', methods=['PATCH'])
@login_required
def material_outreach_material_patch(material_id):
    try:
        material_id = str(material_id or '').strip()
        wx_id = _current_material_outreach_wx_id()
        materials = _load_material_outreach_materials(wx_id)
        material = next((item for item in materials if str((item or {}).get('id') or '').strip() == material_id), None)
        if not material:
            return jsonify({'status': 'error', 'message': '未找到这条素材'}), 404

        payload = request.get_json(silent=True) or {}
        if isinstance(payload, dict) and 'status' in payload:
            status = str(payload.get('status') or '').strip()
            if status in {'active', 'disabled'}:
                material['status'] = status

        if isinstance(payload, dict) and 'ownership' in payload:
            material['ownership'] = normalize_material_ownership(payload.get('ownership'))

        if isinstance(payload, dict) and 'copy_note' in payload:
            material['copy_note'] = _normalize_material_copy_note(payload.get('copy_note'))

        _save_material_outreach_materials(materials, wx_id)
        return jsonify(_material_management_response(wx_id, materials))
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


def _current_prompt_name(config, chat_name):
    if not isinstance(config, dict):
        return '默认'
    listen_list = config.get('listen_list', []) or []
    bindings = config.get('chat_prompt_map', {})
    if isinstance(listen_list, list) and chat_name in listen_list and isinstance(bindings, dict):
        prompt_name = str(bindings.get(chat_name, '') or '').strip()
        if prompt_name:
            return prompt_name
    return str(config.get('default_prompt', '默认') or '默认').strip()


def _chat_memory_user_sort_key(item):
    chat_name = str((item or {}).get('chat_name', '') or '').strip()
    return _wechat_name_sort_key(chat_name)


@app.route('/chat_memory/users')
@login_required
def chat_memory_users():
    """返回已有会话记忆列表。"""
    try:
        config = read_config() or {}
        wx_id = _chat_memory_wx_id_from_request()
        by_name = {}
        if not wx_id:
            return jsonify({'status': 'success', 'wx_id': '', 'users': []})
        for state in _chat_memory_store(wx_id).list_states():
            chat_name = str(state.get('chat_name', '')).strip()
            if chat_name and _chat_memory_state_has_content(state):
                by_name[chat_name] = {
                    'chat_name': chat_name,
                    'wx_id': wx_id,
                    'current_prompt_name': _current_prompt_name(config, chat_name),
                    'updated_at': state.get('updated_at', ''),
                    'source': 'chat_memory',
                }
        users = sorted(by_name.values(), key=_chat_memory_user_sort_key)
        return jsonify({'status': 'success', 'wx_id': wx_id, 'users': users})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/chat_memory/wx_ids')
@login_required
def chat_memory_wx_ids():
    """返回可管理会话记忆的微信号目录。"""
    try:
        return jsonify({'status': 'success', **_account_picker_payload(CHAT_MEMORY_BASE)})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/chat_memory/wx/<wx_id>', methods=['DELETE'])
@login_required
def chat_memory_wx_delete(wx_id):
    """Delete all chat memory documents under one WeChat account namespace."""
    try:
        wx_id = _validate_known_account_wx_id(wx_id, base_dir=CHAT_MEMORY_BASE)
        if not wx_id:
            return jsonify({'status': 'error', 'message': '请先选择微信号'})
        target = os.path.abspath(_account_chat_memory_dir(wx_id))
        base = os.path.abspath(str(account_dir(CHAT_MEMORY_BASE, wx_id).parent))
        if os.path.commonpath([base, target]) != base:
            return jsonify({'status': 'error', 'message': '微信号路径无效'})
        if os.path.isdir(target):
            shutil.rmtree(target)
        return jsonify({'status': 'success', 'wx_id': wx_id, 'message': '已删除该微信号下的会话记忆'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/chat_memory/state/<chat_name>', methods=['GET'])
@login_required
def chat_memory_state_get(chat_name):
    """读取单个用户的会话记忆；不存在时返回默认结构。"""
    try:
        wx_id = _chat_memory_wx_id_from_request()
        if not wx_id:
            return jsonify({'status': 'error', 'message': '请先选择微信号'})
        store = _chat_memory_store(wx_id)
        exists = os.path.exists(store.state_path(chat_name))
        state = store.load_state(chat_name, wx_id=wx_id, strict=True)
        has_chat_record = False
        if wx_id and chat_name:
            chat_dir = os.path.join(_account_memory_dir(wx_id), resolve_memory_storage_name(chat_name))
            has_chat_record = os.path.isdir(chat_dir)
        return jsonify({
            'status': 'success',
            'wx_id': wx_id,
            'exists': exists,
            'state': state,
            'document': state.get('document', ''),
            'has_chat_record': has_chat_record,
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/chat_memory/state/<chat_name>', methods=['POST'])
@login_required
def chat_memory_state_save(chat_name):
    """保存单个用户的会话记忆。"""
    try:
        data = request.get_json(silent=True) or {}
        wx_id = _chat_memory_wx_id_from_request()
        if not wx_id:
            return jsonify({'status': 'error', 'message': '请先选择微信号'})
        store = _chat_memory_store(wx_id)
        if not isinstance(data, dict):
            return jsonify({'status': 'error', 'message': '请求体必须是结构化对象'})
        if 'state' in data and not any(key in data for key in ('memories', 'maintenance', 'chat_name')):
            return jsonify({'status': 'error', 'message': '请求体必须直接传会话记忆结构，不能再包一层 state'})
        state_payload = {
            'schema_version': data.get('schema_version', 2),
            'updated_at': data.get('updated_at', ''),
            'chat_name': chat_name,
            'wx_id': wx_id,
            'memories': _chat_memory_merge_incoming_memories(
                store,
                chat_name,
                wx_id,
                data.get('memories', []),
            ),
            'maintenance': data.get('maintenance', {}),
        }
        saved = store.save_state(chat_name, state_payload, wx_id=wx_id)
        return jsonify({
            'status': 'success',
            'wx_id': wx_id,
            'state': saved,
            'document': saved.get('document', ''),
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/chat_memory/state/<chat_name>', methods=['DELETE'])
@login_required
def chat_memory_state_delete(chat_name):
    """删除单个用户的会话记忆。"""
    try:
        wx_id = _chat_memory_wx_id_from_request()
        if not wx_id:
            return jsonify({'status': 'error', 'message': '请先选择微信号'})
        _chat_memory_store(wx_id).delete_state(chat_name)
        return jsonify({'status': 'success', 'wx_id': wx_id, 'message': '已删除'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/chat_memory/extract/<chat_name>', methods=['POST'])
@login_required
def chat_memory_extract_now(chat_name):
    """手动为选中的用户提取一次会话记忆。"""
    try:
        data = request.get_json(silent=True) or {}
        wx_id = _chat_memory_wx_id_from_request()
        if not wx_id:
            return jsonify({'status': 'error', 'message': '请先选择微信号'})
        chat_name = str(chat_name or '').strip()
        if not chat_name:
            return jsonify({'status': 'error', 'message': '请先选择好友'})
        config = read_config() or {}
        store = _chat_memory_store(wx_id)
        prompt_name = _current_prompt_name(config, chat_name)
        extractor = ChatMemoryExtractor(message_threshold=1, interval_hours=1)

        def _summary_payload(before_state, after_state, proposal, processed_count):
            before_memories = len((before_state.get('memories') or []) if isinstance(before_state, dict) else [])
            after_memories = len((after_state.get('memories') or []) if isinstance(after_state, dict) else [])
            proposal = proposal if isinstance(proposal, dict) else {}
            return {
                'processed_count': int(processed_count or 0),
                'memories_before': before_memories,
                'memories_after': after_memories,
                'add_count': len(proposal.get('add') or []),
                'update_count': len(proposal.get('update') or []),
                'delete_count': len(proposal.get('delete') or []),
            }

        def _build_preview():
            messages = _chat_memory_messages_for_user(wx_id, chat_name)
            if not messages:
                raise ValueError('没有可提取的聊天记录')
            state = store.load_state(chat_name, prompt_name, wx_id=wx_id, strict=True)
            selected_messages = extractor.select_new_messages(state, messages, protected_count=0)
            if not selected_messages:
                raise ValueError('没有新的可提取聊天记录')
            api_config = _get_chat_api_config(config, chat_name)
            api = _build_test_api_client(_build_memory_extraction_api_config(api_config))
            try:
                proposal, _ = extractor.extract_valid_proposal(api, state, selected_messages)
            except ValueError as e:
                error = ValueError(f'AI 返回的会话记忆提案 JSON 格式不正确：{e}')
                setattr(error, 'bad_output', getattr(e, 'bad_output', ''))
                raise error
            merged = store.merge_proposal(
                state,
                proposal,
                chat_name=chat_name,
                wx_id=wx_id,
                now=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            )
            ok, validation_message = store.validate_state(merged)
            if not ok:
                error = ValueError(f'AI 返回的会话记忆提案 JSON 格式不正确：{validation_message}')
                setattr(error, 'bad_output', json.dumps(proposal, ensure_ascii=False, indent=2))
                raise error
            return {
                'state': state,
                'merged': merged,
                'proposal': proposal,
                'processed_count': len(selected_messages),
                'processed_cursor': extractor.processed_cursor(selected_messages),
                'summary': _summary_payload(state, merged, proposal, len(selected_messages)),
            }

        def _save_merged_state(base_state, proposal, processed_cursor, processed_count):
            base_state = store.load_state(chat_name, prompt_name, wx_id=wx_id, strict=True) if base_state is None else base_state
            merged = store.merge_proposal(
                base_state,
                proposal,
                chat_name=chat_name,
                wx_id=wx_id,
                now=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            )
            ok, validation_message = store.validate_state(merged)
            if not ok:
                raise ValueError(f'AI 返回的会话记忆提案 JSON 格式不正确：{validation_message}')
            cursor = processed_cursor if isinstance(processed_cursor, dict) else {}
            if not str(cursor.get('last_processed_message_key', '') or '').strip():
                raise ValueError('缺少本次提取的处理游标，请重新点击“马上提取记忆”。')
            merged.setdefault('maintenance', {})
            merged['maintenance'].update({
                'last_processed_message_key': str(cursor.get('last_processed_message_key', '') or '').strip(),
                'last_processed_message_time': str(cursor.get('last_processed_message_time', '') or '').strip(),
            })
            merged['maintenance']['last_processed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            merged['maintenance']['last_attempted_at'] = merged['maintenance']['last_processed_at']
            saved = store.save_state(chat_name, merged, wx_id=wx_id)
            return jsonify({
                'status': 'success',
                'message': f'已提取 {int(processed_count or 0)} 条聊天记录并更新会话记忆',
                'wx_id': wx_id,
                'chat_name': chat_name,
                'processed_count': int(processed_count or 0),
                'state': saved,
                'document': saved.get('document', ''),
                'proposal': proposal,
                'summary': _summary_payload(base_state, saved, proposal, int(processed_count or 0)),
            })

        if data.get('confirm_apply'):
            state = store.load_state(chat_name, prompt_name, wx_id=wx_id, strict=True)
            base_updated_at = str(data.get('base_updated_at', '') or '').strip()
            if base_updated_at and str(state.get('updated_at', '') or '').strip() != base_updated_at:
                return jsonify({'status': 'error', 'message': '会话记忆内容已变化，请重新点击“马上提取记忆”获取最新预览。'})
            proposal = data.get('proposal') or {}
            ok, validation_message = extractor.validate_proposal(proposal, state=state)
            if not ok:
                return jsonify({
                    'status': 'error',
                    'message': f'AI 返回的会话记忆提案 JSON 格式不正确：{validation_message}',
                    'reply': json.dumps(proposal, ensure_ascii=False, indent=2),
                })
            return _save_merged_state(state, proposal, data.get('processed_cursor') or {}, data.get('processed_count') or 0)

        preview = _build_preview()
        if data.get('preview_only'):
            return jsonify({
                'status': 'success',
                'preview_only': True,
                'message': f'本次将提取 {preview["processed_count"]} 条聊天记录；确认后才会真正更新会话记忆',
                'wx_id': wx_id,
                'chat_name': chat_name,
                'processed_count': preview['processed_count'],
                'state': preview['merged'],
                'current_state': preview['state'],
                'document': preview['merged'].get('document', ''),
                'proposal': preview['proposal'],
                'processed_cursor': preview['processed_cursor'],
                'base_updated_at': str((preview['state'] or {}).get('updated_at', '') or '').strip(),
                'summary': preview['summary'],
            })
        return _save_merged_state(preview['state'], preview['proposal'], preview['processed_cursor'], preview['processed_count'])
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e), 'reply': getattr(e, 'bad_output', '')})


@app.route('/prompt/preview', methods=['POST'])
@login_required
def prompt_preview():
    """预览指定用户最终会发送给 AI 的系统 Prompt。"""
    try:
        data = request.get_json() or {}
        chat_name = str(data.get('chat_name', '') or '').strip()
        message = str(data.get('message', '') or '').strip()
        if not chat_name:
            return jsonify({'status': 'error', 'message': 'chat_name 不能为空'})
        wx_id, wx_error = _explicit_or_single_chat_memory_wx_id(data.get('wx_id', ''))
        if wx_error:
            return jsonify({'status': 'error', 'message': wx_error})
        config = read_config() or {}
        overrides = data.get('config_overrides')
        if isinstance(overrides, dict):
            for key in (
                'default_prompt',
                'chat_prompt_map',
                'listen_list',
                'AllListen_switch',
                'chat_memory_switch',
                'chat_memory_exclude_list',
            ):
                if key in overrides:
                    config[key] = overrides[key]
        prompt_name = _current_prompt_name(config, chat_name)
        base_prompt = ''
        path = os.path.join(PROMPT_DIR, f'{prompt_name}.md')
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                base_prompt = f.read()
        state_dir = _account_chat_memory_dir(wx_id, create=True)
        system = PromptSystem(config, state_dir=state_dir, prompt_dir=PROMPT_DIR)
        prompt = system.build_prompt(chat_name, [], message, base_prompt=base_prompt)
        return jsonify({'status': 'success', 'prompt': prompt, 'prompt_name': prompt_name, 'wx_id': wx_id})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/backup_now', methods=['POST'])
@login_required
def backup_now():
    """立即执行一次数据备份，返回备份路径"""
    try:
        path = _do_backup()
        return jsonify({'status': 'success', 'message': '备份成功！', 'path': path})
    except Exception as e:
        log('ERROR', f'手动备份失败: {e}')
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/memory/list')
@login_required
def memory_list():
    """返回所有微信号目录"""
    try:
        return jsonify({'status': 'success', **_account_picker_payload(MEMORY_BASE)})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

def _safe_is_dir(parent_abs, name):
    """os.path.isdir 在 Windows 上对末尾含 '.' 的名称会自动去掉 '.' 导致误判。
    用 UNC 长路径前缀绕过 Windows 路径规范化，其他系统走普通逻辑。"""
    if os.name == 'nt':
        p = '\\\\?\\' + parent_abs + '\\' + name
    else:
        p = os.path.join(parent_abs, name)
    try:
        import stat as _stat
        return _stat.S_ISDIR(os.stat(p).st_mode)
    except OSError:
        return False


@app.route('/memory/chats/<wx_id>')
@login_required
def memory_chats(wx_id):
    """返回指定微信号下所有窗口名"""
    try:
        wx_path = _account_memory_dir(wx_id)
        if not os.path.exists(wx_path):
            return jsonify({'status': 'success', 'chats': []})
        wx_abs = os.path.abspath(wx_path)
        chats = []
        for d in os.listdir(wx_path):
            if not _safe_is_dir(wx_abs, d):
                continue
            chat_path = os.path.join(wx_path, d)
            if not _memory_chat_dir_has_messages(chat_path):
                continue
            chats.append({'name': read_memory_original_name(chat_path, d), 'storage_name': d})
        chats.sort(key=lambda item: (
            _wechat_name_sort_key(item.get('name')),
            str(item.get('storage_name') or ''),
        ))
        return jsonify({'status': 'success', 'chats': chats})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/memory/data/<wx_id>/<chat_name>')
@login_required
def memory_data(wx_id, chat_name):
    """返回指定窗口的记忆数据（JSON 列表）"""
    try:
        dir_abs = os.path.abspath(_account_memory_dir(wx_id))
        storage_name = resolve_memory_storage_name(chat_name)
        if os.name == 'nt':
            chat_dir = '\\\\?\\' + dir_abs + '\\' + storage_name
        else:
            chat_dir = os.path.join(dir_abs, storage_name)
        if not os.path.exists(chat_dir):
            return jsonify({'status': 'success', 'messages': []})
        # 扫目录找实际的 *_memory.json 文件（Windows 可能截断目录名导致文件名与目录名不一致）
        mem_files = [f for f in os.listdir(chat_dir) if f.endswith('_memory.json')]
        if not mem_files:
            return jsonify({'status': 'success', 'messages': []})
        if os.name == 'nt':
            file_path = '\\\\?\\' + dir_abs + '\\' + storage_name + '\\' + mem_files[0]
        else:
            file_path = os.path.join(chat_dir, mem_files[0])
        with open(file_path, 'r', encoding='utf-8') as f:
            messages = json.load(f)
        visible_messages = [
            format_memory_record_for_display(item) if isinstance(item, dict) else item
            for item in (messages if isinstance(messages, list) else [])
        ]
        return jsonify({'status': 'success', 'messages': visible_messages})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/memory/delete_wx/<wx_id>', methods=['DELETE'])
@login_required
def memory_delete_wx(wx_id):
    """删除指定微信号的所有记忆"""
    try:
        wx_id = _validate_known_account_wx_id(wx_id, base_dir=MEMORY_BASE)
        target_abs = os.path.abspath(_account_memory_dir(wx_id))
        base_abs = os.path.abspath(str(account_dir(MEMORY_BASE, wx_id).parent))
        if os.path.commonpath([base_abs, target_abs]) != base_abs:
            return jsonify({'status': 'error', 'message': '微信号路径无效'}), 400
        if os.name == 'nt':
            wx_path = '\\\\?\\' + target_abs
        else:
            wx_path = target_abs
        if os.path.exists(wx_path):
            shutil.rmtree(wx_path)
        log('SUCCESS', f'已删除微信号 {wx_id} 的所有记忆')
        return jsonify({'status': 'success', 'message': '已删除'})
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/memory/delete_chat/<wx_id>/<chat_name>', methods=['DELETE'])
@login_required
def memory_delete_chat(wx_id, chat_name):
    """删除指定窗口的记忆文件"""
    try:
        wx_id = _validate_known_account_wx_id(wx_id, base_dir=MEMORY_BASE)
        parent_abs = os.path.abspath(_account_memory_dir(wx_id))
        storage_name = resolve_memory_storage_name(chat_name)
        target_abs = os.path.abspath(os.path.join(parent_abs, storage_name))
        if os.path.commonpath([parent_abs, target_abs]) != parent_abs:
            return jsonify({'status': 'error', 'message': '聊天记录路径无效'}), 400
        if os.name == 'nt':
            chat_path = '\\\\?\\' + target_abs
        else:
            chat_path = target_abs
        if os.path.exists(chat_path):
            shutil.rmtree(chat_path)
        log('SUCCESS', f'已删除 {wx_id}/{chat_name} 的记忆')
        return jsonify({'status': 'success', 'message': '已删除'})
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


def time_start_stop():
    """定时启停"""
    def is_target_time(target_hour, target_minute):
        """
        校验当前时间是否匹配指定的小时和分钟
        """
        # 获取当前本地时间
        now = datetime.now()
        # 比较小时和分钟是否匹配
        return (now.hour == target_hour) and (now.minute == target_minute)
    def load_time_schedule_config():
        """读取并校验定时启停配置，非法时间格式时仅跳过调度。"""
        time_config = read_config() or {}
        enabled = bool(time_config.get("everyday_start_stop_bot_switch"))
        if not enabled:
            return False, None, None

        start_time, start_err = _parse_hhmm_config(
            time_config.get("everyday_start_bot_time"),
            "everyday_start_bot_time",
        )
        stop_time, stop_err = _parse_hhmm_config(
            time_config.get("everyday_stop_bot_time"),
            "everyday_stop_bot_time",
        )

        errors = [err for err in (start_err, stop_err) if err]
        if errors:
            for err in errors:
                log('ERROR', f'定时启停配置校验失败: {err}')
            log('WARNING', '定时启停已临时禁用，本轮不会执行，请修正时间格式后重新保存配置')
            return False, None, None

        return True, start_time, stop_time
    def time_check_thread():
        """定时检查线程"""
        global bot_thread, bot, update_config_status
        # 读取配置文件
        start_hour = start_minute = stop_hour = stop_minute = None
        everyday_start_stop_bot_switch, start_time, stop_time = load_time_schedule_config()
        if start_time:
            start_hour, start_minute = start_time
        if stop_time:
            stop_hour, stop_minute = stop_time
        if everyday_start_stop_bot_switch:
            log('INFO', f'启动定时启停线程，启动时间：{start_hour}:{start_minute}，停止时间：{stop_hour}:{stop_minute}')
        else:
            log('INFO', '定时启停未启用，未启用')

        while True:
            if update_config_status: # 保存配置后更新定时启停状态
                update_config_status = False
                start_hour = start_minute = stop_hour = stop_minute = None
                everyday_start_stop_bot_switch, start_time, stop_time = load_time_schedule_config()
                if start_time:
                    start_hour, start_minute = start_time
                if stop_time:
                    stop_hour, stop_minute = stop_time
                if everyday_start_stop_bot_switch:
                    log('INFO', f'配置更新，启动定时启停线程，启动时间：{start_hour}:{start_minute}，停止时间：{stop_hour}:{stop_minute}')
                else:
                    log('INFO', '配置更新，定时启停未启用')
            if everyday_start_stop_bot_switch:
                if is_target_time(start_hour, start_minute): # 启动时间
                    log('INFO', '到达预定启动时间，正在启动机器人')
                    if bot_thread and bot_thread.is_alive():
                        log("WARNING", "状态：机器人已在运行")
                        log(message="定时启动机器人:机器人已在运行，无需启动")
                        # email_send.send_email(subject="定时启动机器人", content="机器人已在运行，无需启动")
                    else:
                        try:
                            result = _start_bot_runtime(wait_timeout=BOT_START_WAIT_TIMEOUT_SECONDS)
                            if result.get('status') == 'success':
                                log(level='INFO', message="定时启动机器人:机器人已启动")
                            elif result.get('status') == 'pending':
                                log(level='INFO', message="定时启动机器人:机器人正在启动，等待微信监听初始化完成")
                            else:
                                log('ERROR', f"定时启动机器人失败：{result.get('message') or '未知错误'}")
                            # email_send.send_email(subject="定时启动机器人", content="机器人已启动")
                        except Exception as e:
                            log('ERROR', f'启动机器人失败: {str(e)}')
                    time.sleep(60) # 防止一分钟内重复启动
                if is_target_time(stop_hour, stop_minute): # 停止时间
                    log('INFO', '到达预定停止时间，正在停止机器人')
                    if bot_thread and bot_thread.is_alive():
                        stopped_ok, stop_message = _stop_running_bot_and_wait()
                        if stopped_ok:
                            log(message="定时停止机器人:机器人已停止")
                            # email_send.send_email(subject="定时停止机器人", content="机器人已停止")
                        else:
                            log('ERROR', f'定时停止机器人失败：{stop_message}')
                    else:
                        log('WARNING', '状态：机器人未运行')
                        log(message="定时停止机器人:机器人未运行，无需停止")
                        # email_send.send_email(subject="定时停止机器人", content="机器人未运行，无需停止")
                    time.sleep(60) # 防止一分钟内重复停止
            time.sleep(10)
    
    time_thread = threading.Thread(target=time_check_thread, daemon=True)
    time_thread.start()
def find_free_port(start_port=10001, max_port=11000):
    """从 start_port 开始寻找空闲端口"""
    for port in range(start_port, max_port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("未找到可用端口")


def main():
    logging.basicConfig(
        level=logging.WARNING,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.NullHandler()]
    )
    # 屏蔽 werkzeug 的 INFO 级别访问日志（如 /get_logs、/get_status 轮询请求）
    # WARNING 及以上（如端口冲突、路由错误）仍正常输出
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    log('INFO', '服务器启动中...')
    try:
        if not os.path.exists(CONFIG_FILE):
            default_config = {
                "api_configs": [
                    {"sdk": "DusAPI", "key": "your-api-key", "url": "https://api.dusapi.com", "model": "gpt-5.4"},
                    {"sdk": "DusAPI", "key": "your-api-key", "url": "https://api.dusapi.com", "model": "claude-sonnet-4-6"},
                ],
                "api_index": 0,
                "api_capability_map": {},
                "admin": "文件传输助手",
                "AllListen_switch": False,
                "AllListen_filter_mute": True,
                "chat_listen_only": False,
                "listen_list": [],
                "global_blacklist": [],
                "group": [],
                "group_api_map": {},
                "group_switch": False,
                "group_listen_only": False,
                "group_reply_at": False,
                "group_reply_at_msg": True,
                "group_reply_quote": True,
                "group_welcome": False,
                "group_welcome_random": 1.0,
                "group_welcome_msg": "欢迎新朋友！请先查看群公告！",
                "new_friend_switch": False,
                "new_friend_archive_switch": True,
                "new_friend_reply_switch": False,
                "new_friend_msg": {"text": "", "files": []},
                "new_friend_check_min": 60,
                "new_friend_check_max": 300,
                "new_friend_remark_use_nickname": True,
                "new_friend_remark_prefix": "",
                "new_friend_remark_prefix_timestamp": False,
                "new_friend_remark_suffix": "_机器人备注",
                "new_friend_remark_suffix_timestamp": False,
                "new_friend_tags": [],
                "chat_keyword_switch": False,
                "group_keyword_switch": False,
                "group_keyword_at_only": False,
                "keyword_dict": {},
                "scheduled_message_task_list": [],
                "contact_directory_auto_maintenance_switch": False,
                "contact_directory_auto_maintenance_batch_size": 50,
                "contact_directory_auto_maintenance_interval_minutes": 20,
                "contact_directory_auto_maintenance_full_scan_interval_days": 7,
                "contact_directory_auto_maintenance_window_start": "00:00",
                "contact_directory_auto_maintenance_window_end": "23:59",
                "material_source_list": [],
                "material_source_pool_limit_map": {},
                "material_source_silent": True,
                "material_outreach_list": [],
                "everyday_start_stop_bot_switch": False,
                "everyday_start_bot_time": "08:00",
                "everyday_stop_bot_time": "23:00",
                "memory_switch": True,
                "memory_context_switch": True,
                "memory_max_count": 5000,
                "memory_context_count": 50,
                "memory_context_assistant_count": 10,
                "reply_delay_switch": True,
                "reply_delay_first_min": 1,
                "reply_delay_first_max": 5,
                "reply_delay_split_speed_mode": "fast",
                "reply_delay_split_min": 1,
                "reply_delay_split_max": 2,
                "clean_ai_reply_switch": True,
                "chat_image_recognition_switch": False,
                "chat_voice_recognition_switch": False,
                "voice_transcription_fallback_text": DEFAULT_VOICE_TRANSCRIPTION_FALLBACK_TEXT,
                "voice_transcription_fallback_reply_once": False,
                "chat_message_merge_delay": 3.0,
                "chat_image_recognition_api": 0,
                "group_image_recognition_switch": False,
                "group_voice_recognition_switch": False,
                "group_image_recognition_api": 0,
                "custom_forward_switch": False,
                "custom_forward_list": [],
                "default_prompt": "默认",
                "chat_prompt_map": {},
                "chat_api_map": {},
                "chat_tts_map": {},
                "group_prompt_map": {},
                "chat_memory_switch": True,
                "chat_memory_exclude_list": [],
                "chat_memory_message_threshold": 100,
                "chat_memory_interval_hours": 12,
                "api_error_reply": "",
                "api_error_reply_once": False,
                "meta_reply_blocked_reply": "",
                "meta_reply_blocked_reply_once": False,
                "text_reply_limit_switch": False,
                "text_reply_limit_count": 99,
                "text_reply_limit_hours": 24,
                "text_reply_limit_ai_reply": True,
                "text_reply_limit_reply": "",
                "text_reply_limit_reply_once": False,
                "chat_split_reply_switch": False,
                "chat_split_max_chars": 100,
                "chat_split_max_count": 4,
                "group_split_reply_switch": False,
                "group_split_max_chars": 100,
                "group_split_max_count": 4,
                "siver_panel_enabled": False,
                "siver_panel_activation_code": "",
                "siver_panel_activation_code_applied_hash": "",
                "siver_panel_activation_code_failed_hash": "",
                "siver_panel_slug": "",
                "siver_panel_install_id": "",
                "siver_panel_machine_fingerprint": "",
                "siver_panel_device_id": "",
                "siver_panel_device_secret": "",
                "siver_panel_base_url": SIVER_PANEL_BASE_URL,
                "siver_panel_ws_url": SIVER_PANEL_WS_URL,
                "siver_panel_panel_url": "",
                "siver_panel_service_expire_at": "",
                "siver_panel_last_error_code": "",
                "siver_panel_last_error_message": "",
            }
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(default_config, f, ensure_ascii=False, indent=4)
            _ensure_prompt_dir()
            log('WARNING', '配置文件不存在，已创建默认配置文件')
        log('INFO', '服务5s后启动')
        # 启动时自动备份检查
        try:
            _check_and_auto_backup()
        except Exception as _backup_e:
            log('ERROR', f'自动备份检查失败: {_backup_e}')
        # 动态选择端口
        global panel_server_port
        free_port = find_free_port(10001, 11000)
        panel_server_port = free_port
        log('INFO', f'请访问 http://localhost:{free_port} 或者 http://127.0.0.1:{free_port} 进行登录')
        # 启动后自动打开浏览器
        webbrowser.open(f"http://127.0.0.1:{free_port}")
        # 定时启停
        time_start_stop()
        if siver_panel_manager is not None:
            siver_panel_manager.set_local_port_provider(get_panel_server_port)
            siver_panel_manager.start()
        # 启动服务器
        app.run(host='127.0.0.1', port=free_port, debug=False, threaded=True)
    except Exception as e:
        log('ERROR', f'服务器启动失败: {str(e)}')
    finally:
        log('INFO', '服务器已停止')

if __name__ == '__main__':
    main()
