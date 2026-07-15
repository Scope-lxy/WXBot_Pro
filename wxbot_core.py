#!/usr/bin/env python3
# Siver微信机器人 siver_wxbot - 面向对象版本 - wxautox4版本
# 作者：https://www.siver.top

version = "V4.7.27"
version_log = "V4.7.27 - 合并远程访问凭据恢复与内外网访问优化，保留本地接口测试能力"
custom_build = True
update_feed_url = "https://wxbot.siverking.online/version.json"
update_source_name = "官方版本源"
source_repo_url = "https://github.com/SiverKing/SiverWXbot_plus"
release_url = "https://github.com/SiverKing/SiverWXbot_plus/releases"
download_url = "https://wwbuf.lanzout.com/b00tcdnlte"

# ============================================================
# 标准库导入
# ============================================================
import os
import re
import sys
import time
import json
import hashlib
import random
import sqlite3
import threading
from contextlib import nullcontext
import traceback
import uuid
from collections import deque
from queue import Empty, Queue
from datetime import datetime, timedelta
from types import SimpleNamespace

# ============================================================
# 第三方库导入
# ============================================================
import requests
import schedule                  # 定时任务库
try:
    import pythoncom
except Exception:
    pythoncom = None

# ============================================================
# wxautox 相关导入（Plus版，需向作者购买授权）
# 购买地址：https://www.siverking.online/static/img/siver_wx.jpg
# ============================================================
from wxautox4 import WxParam
from wxautox4.utils.useful import check_license

is_wxautox = True  # 标识当前使用的是 wxautox Plus 版本

# ============================================================
# 本地模块导入
# ============================================================
from extension import email as email_send
from core.api import (
    API_ERROR_REPLY_TEXT,
    APIConfigSnapshot,
    DusAPI,
    OpenAIAPI,
    build_api_config_snapshot,
    default_tts_config,
    format_api_display_name,
    normalize_tts_settings,
    is_api_error_reply,
    select_tts_config,
    set_chat_api_app_version,
)
from core.logger import install_thread_exception_logger, log, set_thread_exception_observer
from core.wechat_observability import warn_slow_wechat_ui_action
from core.prompt_system import (
    ChatMemoryStore,
    PromptSystem,
    SystemPromptStore,
)
from core.config import api_supports_capability, coerce_float_range, coerce_int_range
from core.account_storage import (
    DEFAULT_ACCOUNT_ID,
    account_file,
    account_area_dir,
    account_module_dir,
    account_module_file,
    ensure_default_account,
    migrate_default_account,
    resolve_account_id,
)
from core import wechat_ui_actions
from core.wechat_ui_runtime import MessageLocateError, OwnedChat, UIClientFacade, WeChatUIRuntime
from core.inbound_coordinator import InboundCoordinator, InboundEvent
from core.message_store import (
    DEFAULT_REPLY_TTL_SECONDS,
    MessageStore,
    MessageStoreTransitionError,
    SQLiteUIDeliveryJournal,
)
from core.reply_delivery import (
    DeliveryNotStarted,
    DeliveryResult,
    DeliveryStatus,
    ReplyAction,
    ReplyDeliveryCoordinator,
    ReplyEchoTracker,
    ReplyKind,
    ReplySource,
    ReplyTurn,
    is_retryable_sqlite_error,
)
from core.chat_history_format import (
    build_model_visible_history,
    format_history_message,
)
from core.memory_context_repair import (
    DEFAULT_CONTEXT_REPAIR_RETRY_SECONDS,
    DEFAULT_LOCAL_HISTORY_LIMIT,
    DEFAULT_VISIBLE_LIMIT,
    normalize_wechat_snapshot,
    snapshot_before_current,
)
from core.reply_count_store import ReplyCountStore
from core.wechat_window import run_with_wechat_rebind_retry
from core.runtime_metrics import RuntimeMetricsStore
from core.reply_pipeline import ImageReplyPipeline, ImageReplyRequest
from core.prompting import (
    IMAGE_DESCRIPTION_SYSTEM_PROMPT,
    build_current_turn_user_message,
    build_image_description_prompt,
    build_image_recognition_message,
    build_image_user_message,
)
from core.vision_bridge import VisionBridge
from core.memory import MemoryManager
from core.contact_profiles import (
    reconcile_contact_storage_names,
    sync_identity_calibration_from_directory,
)
from core.media import cleanup_wxauto_save_cache, existing_local_image_path, is_image_path
from core.message_pipeline import (
    ConversationRef,
    MessageEnvelope,
    MAX_MERGED_PRIVATE_IMAGES,
    QUOTE_IMAGE_MARKER,
    format_model_message_text,
    format_message_semantic_text,
    build_merged_private_message,
    message_content_fingerprint,
    message_unique_id,
    split_quoted_image_message,
    strip_group_mention,
    strip_message_shell,
    strip_voice_duration_metadata,
)
from core.scheduled_tasks import (
    advance_task_plan_after_success,
    compile_task_plan,
    is_task_due,
    iter_enabled_tasks,
    normalize_fixed_task_schedule,
    normalize_random_task_schedule,
)
from core.sending import (
    clean_ai_reply_text,
    describe_reply_preprocess_rejection,
    evaluate_reply_preprocess_admission,
    prepare_reply_parts,
    prepare_reply_parts_with_source,
    reply_preprocess_rejection_label,
    sanitize_ai_output_text,
)
from core.tts import create_tts_client, make_tts_cache_path
from core.wxbot_config import LONG_REPLY_SEGMENT_CHARS, WXBotConfig
from feature.voice_reply import DEFAULT_CHAT_VOICE_REPLY_KEYWORDS, DEFAULT_GROUP_VOICE_REPLY_KEYWORDS
from feature.voice_reply import (
    VoiceReplyLimiter,
    build_tts_context_text,
    classify_voice_reply_text,
    group_voice_candidate,
    is_text_suitable_for_voice,
    load_voice_reply_state,
    normalize_text_for_tts,
    private_voice_candidate,
    save_voice_reply_state,
)
from core.contact_profiles import (
    contact_display_label,
    contact_send_target,
    directory_path as contact_directory_path,
    load_directory as load_contact_directory,
    merge_directory as merge_contact_directory,
    resolve_manual_target_names,
    resolve_target_selector,
    save_directory as save_contact_directory,
)
from feature import contacts, friend_request, listening, message_routing, relationship_scan


PRIVATE_PENDING_VISUAL_CONTEXT_TTL_SECONDS = 600
GROUP_PENDING_VISUAL_CONTEXT_TTL_SECONDS = 7200
GROUP_VISUAL_BATCH_GAP_SECONDS = 120
PENDING_VISUAL_DIRECT_REFERENCE_RE = re.compile(
    r"("
    r"图片|图像|图表|地图|照片|相片|截图|截屏|屏幕截图|画面|画作|"
    r"表情包?|二维码|条形码|海报|菜单|票据|发票|单据|账单|收据|"
    r"(?:这|那|哪|一|两|几|每|上|下|前|后|看|识|张|幅)图|"
    r"图(?:里|中|上|下|呢|吗|是|有|写|显示)|(?:这|那|张)票|"
    r"\b(?:image|picture|photo|photograph|screenshot|screen\s*shot|pic|img|meme|poster|"
    r"qr\s*code|barcode)\b"
    r")",
    re.IGNORECASE,
)
PENDING_VISUAL_CONTEXT_REFERENCE_RE = re.compile(
    r"("
    r"这(?:个|些|张|幅)?|那(?:个|些|张|幅)?|哪(?:个|些|张|幅)?|"
    r"刚才(?:的|那个|那张)?|上面|下面|前面|后面|上一张|下一张|"
    r"\b(?:this|this\s+one|that|that\s+one|it|above|below|previous|last\s+one|next|the\s+one\s+above)\b"
    r")",
    re.IGNORECASE,
)
PENDING_VISUAL_ACTION_RE = re.compile(
    r"("
    r"看看|看一下|看下|读一下|读下|识别|解释|翻译|描述|分析|提取|"
    r"是什么|是啥|有什么|有啥|写着|写的|显示|什么意思|啥意思|什么含义|啥含义|"
    r"\b(?:ocr|read|describe|analy[sz]e|recognize|identify|transcribe|extract|translate|"
    r"explain|caption|what(?:'s| is)|what does|mean|say|says|text)\b"
    r")",
    re.IGNORECASE,
)
PENDING_VISUAL_STANDALONE_ACTION_RE = re.compile(
    r"^\s*(?:(?:请|帮我|麻烦|能不能|可以)\s*)?(?:"
    r"看看|看一下|看下|读一下|读下|识别(?:一下|下)?|解释(?:一下|下)?|"
    r"翻译(?:一下|下)?|描述(?:一下|下)?|分析(?:一下|下)?|提取(?:一下|下)?|"
    r"(?:please\s+)?(?:ocr|read|describe|analy[sz]e|recognize|identify|transcribe|"
    r"extract|translate|explain|caption)(?:\s+(?:it|this|that))?"
    r")(?:\s*(?:可以吗|行吗|谢谢|please))?[？?！!。.]*\s*$",
    re.IGNORECASE,
)
CHAT_MEMORY_BACKGROUND_INTERVAL_SECONDS = 30
PRIVATE_MESSAGE_PIPELINE_MAX_QUEUED_BATCHES = 1
from feature import runtime_task_runner
from feature.keyword_reply import (
    normalize_keyword_reply_actions,
    plan_private_keyword_reply,
)
from feature.material_outreach import (
    DEFAULT_AI_PREFACE_GOAL,
    build_ai_candidate_material_cards,
    build_custom_material_preface,
    build_progress_record,
    build_send_record,
    build_skip_record,
    build_material_entry,
    build_stable_material_signature,
    build_target_snapshot,
    collect_material_source_message,
    iter_enabled_material_outreach_tasks,
    is_forwardable_material_message,
    is_material_source,
    is_target_in_cooldown,
    iter_material_outreach_listen_sources,
    is_forward_result_success,
    load_json_list,
    load_json_object,
    execute_material_outreach_task,
    material_pool_limit_for_source,
    material_title,
    trim_material_pool_by_source,
    material_random_time_window,
    material_sources_for_task,
    material_type_label,
    normalize_material_record,
    normalize_material_outreach_task,
    normalize_material_outreach_preface_config,
    normalize_trigger_strategy,
    normalize_target_selector,
    normalize_material_outreach_history_payload,
    normalize_material_outreach_runtime_payload,
    normalize_material_source_pool_limit_map,
    plan_material_outreach_batches,
    plan_random_material_outreach_fire_time,
    prepare_random_material_outreach_day,
    rebuild_material_pool_for_source,
    save_json_list,
    save_json_object,
    send_names_from_target_snapshot,
    trigger_random_material_outreach_if_due,
)
from feature.material_outreach_preface import (
    build_preface_queue_record,
    due_prefetch_records,
    due_send_records,
    mark_preface_failed,
    mark_preface_generated,
    normalize_preface_pending_queue,
)
from feature.material_outreach_storage import MaterialOutreachStorage
from feature.ai_material_outreach import (
    AI_AUTO_OUTREACH_TASK_ID,
    AI_AUTO_OUTREACH_TASK_NAME,
    build_ai_outreach_candidates_for_target,
    build_ai_pending_record,
    cancel_ai_pending_record,
    cancel_ai_pending_records,
    clear_ai_detection_target,
    clear_ai_detection_target_if_matches,
    describe_ai_outreach_sensitivity,
    due_ai_pending_records,
    evaluate_ai_outreach_gate,
    expire_ai_pending_records,
    filter_ai_outreach_candidate_pool,
    normalize_ai_auto_outreach_runtime_config,
    normalize_ai_detection_record,
    normalize_ai_detection_state,
    normalize_ai_material_outreach_config,
    parse_ai_outreach_decision,
    record_ai_detection_message,
    should_trigger_ai_detection,
)
from feature.scheduled_messages import (
    should_send_scheduled_message,
)
from feature.scheduled_message_tasks import (
    STATUS_PENDING,
    apply_scheduled_message_run_result,
    deserialize_scheduled_message_task_collection,
    ensure_scheduled_message_next_run,
    is_scheduled_message_task_due,
    mark_scheduled_message_running,
    normalize_scheduled_message_task_payload,
    serialize_scheduled_message_task_collection,
    split_scheduled_message_task_storage,
)
from feature.task_workbench_storage import TaskWorkbenchStorage, file_lock_for_path

install_thread_exception_logger()

set_chat_api_app_version(version)

# ============================================================
# wxautox 全局参数配置
# 说明：
#   MESSAGE_HASH         - 是否启用消息哈希辅助判断，开启后稍微影响性能，默认 False
#   FORCE_MESSAGE_XBIAS  - 是否每次启动都重新自动获取 X 偏移量，默认 False
# 其他可配置参数（供参考，未在此处修改）：
#   ENABLE_FILE_LOGGER        (bool) : 是否启用日志文件，默认 True
#   DEFAULT_SAVE_PATH         (str)  : 下载文件/图片默认保存路径
#   DEFAULT_MESSAGE_XBIAS     (int)  : 头像到消息 X 偏移量，默认 51
#   LISTEN_INTERVAL           (int)  : 监听消息时间间隔（秒），默认 1
#   LISTENER_EXCUTOR_WORKERS  (int)  : 监听执行器线程池大小，默认 4
#   SEARCH_CHAT_TIMEOUT       (int)  : 搜索聊天对象超时时间（秒），默认 5
# ============================================================
WxParam.MESSAGE_HASH = True         # 启用消息哈希，辅助消息去重判断
WxParam.FORCE_MESSAGE_XBIAS = True  # 每次启动强制重新获取 X 偏移量
WxParam.DEFAULT_MESSAGE_XBIAS = 50
WxParam.DEFAULT_MESSAGE_YBIAS = 30
WxParam.LISTENER_EXCUTOR_WORKERS = 1  # 保持同一批消息的回调顺序
WXAUTO_SAVE_DIR_NAME = "wxauto_save"


def _wxbot_runtime_base_dir():
    return os.path.dirname(sys.executable) if hasattr(sys, "_MEIPASS") else os.path.abspath(".")


WxParam.DEFAULT_SAVE_PATH = os.path.join(_wxbot_runtime_base_dir(), WXAUTO_SAVE_DIR_NAME)


def detect_local_ffmpeg_paths(base_dir=None):
    root = str(base_dir or _wxbot_runtime_base_dir() or "").strip()
    if not root:
        return None
    bin_dir = os.path.join(root, "venv", "tools", "ffmpeg", "bin")
    ffmpeg_path = os.path.join(bin_dir, "ffmpeg.exe")
    ffprobe_path = os.path.join(bin_dir, "ffprobe.exe")
    if not (os.path.isfile(ffmpeg_path) and os.path.isfile(ffprobe_path)):
        return None
    return {
        "ffmpeg_path": ffmpeg_path,
        "ffprobe_path": ffprobe_path,
        "bin_dir": bin_dir,
    }


def _prepend_runtime_path(path_value):
    value = str(path_value or "").strip()
    if not value:
        return
    current = os.environ.get("PATH", "")
    normalized = os.path.normcase(os.path.normpath(value))
    entries = [item for item in current.split(os.pathsep) if item]
    if any(os.path.normcase(os.path.normpath(item)) == normalized for item in entries):
        return
    os.environ["PATH"] = value if not current else value + os.pathsep + current


def configure_local_ffmpeg_for_wxauto(base_dir=None):
    resolved = detect_local_ffmpeg_paths(base_dir)
    if not resolved:
        return None
    WxParam.AUDIO_PARAM["ffmpeg_path"] = resolved["ffmpeg_path"]
    WxParam.AUDIO_PARAM["ffprobe_path"] = resolved["ffprobe_path"]
    _prepend_runtime_path(resolved["bin_dir"])
    return resolved


configure_local_ffmpeg_for_wxauto()

IMAGE_PARSE_PROMPT_FILE = "image_parse.md"
CLOSING_REPLY_PROMPT_FILE = "closing_reply.md"
MATERIAL_OUTREACH_DECISION_PROMPT_FILE = "material_decision.md"
MATERIAL_OUTREACH_PREFACE_PROMPT_FILE = "material_preface.md"
PRIMARY_CHAT_API_RECOVERY_CHECK_INTERVAL_SECONDS = 30 * 60
VOICE_TRANSCRIPTION_RETRY_DELAY_SECONDS = 5
VOICE_TRANSCRIPTION_MAX_REREAD_ATTEMPTS = 2


class _ChatAPIFailoverProxy:
    def __init__(self, api, chat_callable):
        self._api = api
        self._chat_callable = chat_callable

    def __getattr__(self, name):
        return getattr(self._api, name)

    def chat(self, *args, **kwargs):
        return self._chat_callable(*args, **kwargs)


class _CountingAPIProxy:
    def __init__(self, api, record_request):
        self._api = api
        self._record_request = record_request

    def __getattr__(self, name):
        return getattr(self._api, name)

    def chat(self, *args, **kwargs):
        self._record_request()
        return self._api.chat(*args, **kwargs)


class _ReplyTurnAborted(RuntimeError):
    """The current turn reached a terminal no-fallback delivery state."""

    def __init__(self, status):
        self.status = status
        super().__init__(str(getattr(status, "value", status) or "cancelled"))


# ============================================================
# 配置管理类
# ============================================================
class WXBot:
    """
    微信机器人主类
    整合配置管理、AI 接口、微信监听、消息处理、命令分发等核心功能。
    """

    def __init__(self):
        self.ver      = version
        self.ver_log  = version_log
        self._runtime_instance_id = uuid.uuid4().hex
        self.run_flag = True                    # 主循环运行标志
        self._stop_requested = threading.Event()
        self.config   = WXBotConfig()           # 加载配置
        self._ui_owner = None
        self._ui_runtime = None
        self._ui_watchdog = None
        self._ui_identity = {}
        self._ui_ingress_queue = Queue()
        self._ui_ingress_stop = threading.Event()
        self._ui_ingress_ready = threading.Event()
        self._ui_ingress_thread = None
        self._stop_cleanup_lock = threading.Lock()
        self._stop_cleanup_done = False
        self._message_store = None
        self._inbound_coordinator = None
        self._reply_delivery_coordinator = None
        self._reply_echo_tracker = ReplyEchoTracker(
            text_ttl=DEFAULT_REPLY_TTL_SECONDS,
        )
        self._pending_message_recovery = []
        self._voice_reply_state = load_voice_reply_state(self._voice_reply_state_path())
        self._text_reply_limit_warning_keys = set()
        self._voice_reply_limit_warning_keys = set()

        # 根据当前默认接口快照选择对应的 AI 接口
        self.api = self._init_api()
        self.api_cache = {}                     # 群组专属接口缓存 {api_index: api_instance}
        self._chat_api_failover_lock = threading.RLock()
        self.active_chat_api_index = int(getattr(self.config, 'api_index', 0) or 0)
        self.chat_api_fail_count = 0
        self.chat_api_using_backup = False
        self.next_primary_chat_api_probe_at = None

        self.wx                  = None         # WeChat 客户端对象（延迟初始化）
        self._random_msg_state        = {}     # 随机定时消息运行状态缓存 {task_id: state_dict}
        self._material_runtime_messages = {}
        self._material_source_read_strategies = {}
        self._random_material_outreach_state = {}
        self._runtime_task_reload_lock = threading.RLock()
        self._runtime_task_reload_requested = False
        self._set_material_outreach_namespace()
        self._pause_chat_reply        = False  # 暂停私聊 AI 自动回复标志
        self._pause_group_reply       = False  # 暂停群聊 AI 自动回复标志
        self.memory_manager      = None         # 记忆管理器（init_wx_listeners 时创建）
        self.all_Mode_listen_list = []           # 全局模式下的动态监听列表，元素格式：[昵称, 最新消息时间戳]
        self._listen_chats       = {}
        self._listener_reconcile_interval_seconds = 30
        self._listener_reconcile_last_at = 0.0
        self._listener_auto_recovery_active = False
        self._listener_auto_recovery_attempted = False
        self._listener_auto_recovery_probe_after = 0.0
        self._listener_auto_recovery_last_error = ""
        self._listener_auto_recovery_source = ""
        self.wx_id               = None
        self.start_time          = datetime.now()
        self.callback_is_die     = False        # 回调函数是否发生致命错误的标志
        self.msgs_path           = './wx_msgs/' # 消息本地存储路径（当前未启用）

        # 运行统计数据（供状态面板采集）
        self.msg_received_count  = 0            # 已接收消息数
        self.msg_replied_count   = 0            # 已回复消息数
        self.last_msg_time       = None         # 最近一条消息的时间字符串
        self.last_msg_sender     = None         # 最近一条消息的发送者

        # 私聊回复轮数计数器
        _base = os.path.dirname(sys.executable) if hasattr(sys, '_MEIPASS') else os.path.abspath(".")
        _data = os.path.join(_base, 'data')
        self.reply_count_store = ReplyCountStore(os.path.join(_data, 'config', 'reply_count.json'))
        self._runtime_metrics_store_instance = RuntimeMetricsStore(
            os.path.join(_data, 'config', 'runtime_metrics_v1.json')
        )
        self._init_prompt_system()
        self._chat_merge_lock = threading.Lock()
        self._material_source_read_locks = {}
        self._material_source_read_locks_guard = threading.Lock()
        self._last_incoming_message_at = 0.0
        self._private_message_pipelines = {}
        self._group_message_pipelines = {}
        self._memory_context_repair_state = {}
        self._memory_context_repair_lock = threading.Lock()
        self._pending_private_voice_transcription = {}
        self._chat_memory_dirty_lock = threading.Lock()
        self._chat_memory_dirty_chats = {}
        self._chat_memory_worker_running = False
        self._start_wxauto_save_cache_cleanup()
        set_thread_exception_observer(self._handle_background_thread_exception)

    def _handle_background_thread_exception(self, args):
        thread_name = str(getattr(getattr(args, "thread", None), "name", "") or "")
        if "_listener_listen" not in thread_name or self.is_stop_requested():
            return False
        exc = getattr(args, "exc_value", None)
        if self._arm_listener_auto_recovery(exc, source="wxautox监听线程"):
            return True
        self.callback_is_die = True
        return False

    def _initialize_message_runtime(self, wx_id):
        account_id = str(wx_id or "").strip() or DEFAULT_ACCOUNT_ID
        store = MessageStore(self.config.DATA_DIR, account_id)
        startup = store.recover_startup()
        ui_journal = SQLiteUIDeliveryJournal(store)
        interrupted_ui = ui_journal.freeze_interrupted()
        if getattr(self, "_ui_owner", None) is not None:
            self._ui_owner.set_delivery_journal(ui_journal)
        self._message_store = store
        self._inbound_coordinator = InboundCoordinator(store)
        self._reply_echo_tracker = ReplyEchoTracker(
            store=store,
            text_ttl=DEFAULT_REPLY_TTL_SECONDS,
        )
        self._reply_delivery_coordinator = ReplyDeliveryCoordinator(
            store=store,
            version_provider=lambda conversation, chat_type="private": store.conversation_version(
                conversation,
                chat_type=chat_type,
            ),
            prepare=self._prepare_reply_delivery,
            sender=self._send_reply_delivery,
        )
        self.memory_manager = MemoryManager(
            account_id,
            self.config.DATA_DIR,
            message_store=store,
        )

        replay_event_ids = set()
        recovery = []
        for job in startup.get("replay_jobs", []):
            event_ids = tuple(str(item or "") for item in job.get("event_ids", []) if str(item or ""))
            events = [store.get_event(event_id) for event_id in event_ids]
            events = [event for event in events if event]
            if not events:
                continue
            replay_event_ids.update(event_ids)
            recovery.append({"job": dict(job), "events": events})
        recovery_cursor = 0
        while True:
            page = store.recover_pending_inbound(
                limit=1000,
                after_event_seq=recovery_cursor,
            )
            if not page:
                break
            for event in page:
                if event["event_id"] not in replay_event_ids:
                    recovery.append({"job": None, "events": [event]})
            recovery_cursor = max(int(event["event_seq"]) for event in page)
            if len(page) < 1000:
                break
        self._pending_message_recovery = self._coalesce_message_recovery(recovery)

        uncertain = len(startup.get("uncertain_action_ids", []))
        expired = len(startup.get("expired_job_ids", []))
        if uncertain:
            log(level="WARNING", message=f"消息恢复：{uncertain} 个发送动作结果未知，已禁止自动重发")
        if expired:
            log(level="INFO", message=f"消息恢复：{expired} 个回复已超过 15 分钟有效期，已丢弃")
        if interrupted_ui:
            log(level="WARNING", message=f"微信发送恢复：{len(interrupted_ui)} 个 UI 动作结果未知，已禁止自动重发")
        return store

    @staticmethod
    def _coalesce_message_recovery(items):
        ordered = sorted(
            (item for item in items or [] if item.get("events")),
            key=lambda item: min(int(event.get("event_seq", 0) or 0) for event in item["events"]),
        )
        recovery = []
        for item in ordered:
            current = {"job": item.get("job"), "events": list(item["events"])}
            first = current["events"][0]
            previous = recovery[-1] if recovery else None
            if (
                current["job"] is None
                and previous is not None
                and previous["job"] is None
                and str(first.get("chat_type", "private") or "private") == "private"
                and str(previous["events"][-1].get("chat_type", "private") or "private") == "private"
                and str(previous["events"][-1].get("conversation", "") or "")
                == str(first.get("conversation", "") or "")
                and int(previous["events"][-1].get("event_seq", 0) or 0) + 1
                == int(first.get("event_seq", 0) or 0)
            ):
                previous["events"].extend(current["events"])
            else:
                recovery.append(current)
        return recovery

    def _register_ui_listener_names(self, names):
        owner = getattr(self, "_ui_owner", None)
        if owner is None:
            return []
        registered = []
        seen = set()
        for item in names or []:
            conversation = (
                item
                if isinstance(item, ConversationRef)
                else ConversationRef(str(item or "").strip(), "private")
            )
            key = (conversation.chat_type, conversation.who)
            if not conversation.who or key in seen:
                continue
            seen.add(key)
            owner.call(
                wechat_ui_actions.UIIntent(
                    wechat_ui_actions.UIIntentKind.ADD_LISTEN,
                    {
                        "conversation": conversation.who,
                        "chat_type": conversation.chat_type,
                    },
                ),
                wechat_ui_actions.UI_CALL_WAIT_TIMEOUT,
            )
            registered.append(conversation)
        return registered

    @staticmethod
    def _received_timestamp(value):
        if isinstance(value, datetime):
            return value.timestamp()
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            timestamp = 0.0
        return timestamp if timestamp > 0 else time.time()

    def _match_reply_echo(self, conversation, message, *, chat_type="private"):
        if str(getattr(message, "attr", "") or "").strip().lower() != "self":
            return None
        tracker = getattr(self, "_reply_echo_tracker", None)
        if tracker is None:
            return None
        return tracker.match(
            conversation,
            getattr(message, "type", "text"),
            getattr(message, "content", ""),
            chat_type=chat_type,
        )

    def _build_inbound_event(self, conversation, message):
        source = str(getattr(message, "_wxbot_ingress_source", "") or "subwindow").strip() or "subwindow"
        source_batch = str(getattr(message, "_wxbot_source_batch", "") or "").strip()
        if not source_batch:
            source_batch = f"{source}:{uuid.uuid4().hex}"
            setattr(message, "_wxbot_source_batch", source_batch)
        received_at = self._received_timestamp(getattr(message, "_wxbot_received_at", 0.0))
        raw_content = str(getattr(message, "content", "") or "")
        original_content = str(getattr(message, "original_content", "") or raw_content)
        message_type = str(getattr(message, "type", "") or "text").strip().lower() or "text"
        image_paths = tuple(self._extract_message_image_paths(message))
        stored_content = "[图片]" if message_type == "image" else raw_content
        if message_type == "voice":
            stored_content = strip_voice_duration_metadata(stored_content)
        echo = self._match_reply_echo(
            conversation.who,
            message,
            chat_type=conversation.chat_type,
        )
        if echo is not None:
            stored_content = echo.content
            original_content = echo.content
        related_delivery_id = echo.action_id if echo is not None else ""
        if echo is not None and echo.confirmable and self._message_store is not None:
            try:
                self._message_store.confirm_outbound(
                    echo.action_id,
                    conversation.who,
                    content=echo.content,
                    sent_at=received_at,
                    chat_type=conversation.chat_type,
                    message_type=(
                        "voice" if echo.kind == ReplyKind.VOICE
                        else "file" if echo.kind == ReplyKind.FILE
                        else "text"
                    ),
                )
            except Exception as exc:
                if self._message_store.delivery_action_status(echo.action_id) != "done":
                    log(level="WARNING", message=f"机器人发送回声确认失败：{exc}")
        return InboundEvent(
            conversation=conversation.who,
            chat_type=conversation.chat_type,
            content=stored_content,
            original_content=original_content,
            message_type=message_type,
            sender=str(getattr(message, "sender", "") or ""),
            native_attr=str(getattr(message, "attr", "") or ""),
            native_id=getattr(message, "id", "") or "",
            native_hash=str(getattr(message, "hash", "") or ""),
            native_hash_text=str(getattr(message, "hash_text", "") or ""),
            native_time=str(getattr(message, "time", "") or ""),
            received_at=received_at,
            source=source,
            source_batch=source_batch,
            source_order=int(getattr(message, "window_order", 0) or 0),
            related_delivery_id=related_delivery_id,
            image_paths=image_paths,
        )

    @staticmethod
    def _restore_message_envelope(event):
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        content = str(event.get("content", "") or "")
        image_paths = [str(path or "") for path in metadata.get("image_paths", []) if str(path or "")]
        if str(event.get("message_type", "") or "").lower() == "image" and image_paths:
            content = image_paths[0]
        message = MessageEnvelope(
            content=content,
            original_content=str(event.get("original_content", "") or content),
            type=str(event.get("message_type", "") or "text"),
            sender=str(event.get("sender", "") or ""),
            attr=str(event.get("native_attr", "") or "friend"),
            id=str(event.get("native_id", "") or ""),
            hash=str(event.get("native_hash", "") or ""),
            hash_text=str(event.get("native_hash_text", "") or ""),
            time=str(event.get("native_time", "") or ""),
            _wxbot_ingress_source="startup_recovery",
            _wxbot_received_at=float(event.get("received_at", 0.0) or 0.0),
        )
        message._wxbot_event_id = str(event.get("event_id", "") or "")
        message._wxbot_event_ids = (message._wxbot_event_id,)
        message._wxbot_event_version = int(event.get("conversation_version", 0) or 0)
        message._wxbot_reply_expires_at = float(event.get("reply_expires_at", 0.0) or 0.0)
        message._wxbot_startup_recovery = True
        if image_paths:
            message._wxbot_media_prepared = True
        return message

    def _drain_message_recovery(self):
        recovery = list(getattr(self, "_pending_message_recovery", []) or [])
        self._pending_message_recovery = []
        recovered = 0
        for item in recovery:
            events = list(item.get("events") or [])
            if not events:
                continue
            job = item.get("job") if isinstance(item.get("job"), dict) else None
            conversation = str(events[0].get("conversation", "") or "")
            chat_type = str(events[0].get("chat_type", "private") or "private")
            messages = [self._restore_message_envelope(event) for event in events]
            if job:
                for message in messages:
                    turn_id = str(job.get("turn_id", "") or "")
                    message._wxbot_recovery_turn_id = turn_id
                    message._wxbot_reply_turn_id = turn_id
                    message._wxbot_recovery_route_source = str(
                        job.get("route_source", "") or ""
                    )
                    message._wxbot_event_version = int(job.get("expected_version", 0) or 0)
                    message._wxbot_reply_expires_at = float(job.get("expires_at", 0.0) or 0.0)
            chat = OwnedChat(self._ui_owner, conversation, chat_type)
            for message in messages:
                message_routing.prepare_message_media(self, message, chat)
                if getattr(message, "_wxbot_media_prepared", False):
                    self._enrich_persisted_ui_message(ConversationRef(conversation, chat_type), message)
            if job and any(
                getattr(message, "_skip_ai_reply", False)
                and not getattr(message, "_wxbot_pending_voice_key", "")
                for message in messages
            ):
                self._cancel_unfinished_reply_job(messages[0], "recovered media is no longer replyable")
                continue
            replyable = []
            for message in messages:
                if (
                    getattr(message, "_skip_ai_reply", False)
                    and not getattr(message, "_wxbot_pending_voice_key", "")
                ):
                    self._mark_inbound_no_reply(message)
                    continue
                replyable.append(message)
            messages = replyable
            if not messages:
                continue
            has_pending_voice = any(
                getattr(message, "_wxbot_pending_voice_key", "") for message in messages
            )
            if chat_type == "private" and has_pending_voice:
                current_voice_keys = {
                    str(getattr(message, "_wxbot_pending_voice_key", "") or "")
                    for message in messages
                    if getattr(message, "_wxbot_pending_voice_key", "")
                }
                with self._chat_merge_lock:
                    pipeline = self._private_message_pipeline(conversation)
                    earlier = [
                        message
                        for message in pipeline["open_messages"]
                        if str(getattr(message, "_wxbot_pending_voice_key", "") or "")
                        not in current_voice_keys
                    ]
                    pipeline["open_messages"] = earlier + messages
                    kinds = {
                        self._private_message_batch_kind(message)
                        for message in pipeline["open_messages"]
                    }
                    pipeline["open_kind"] = kinds.pop() if len(kinds) == 1 else "mixed"
                recovered += 1
                continue
            if chat_type == "private" and len(messages) > 1 and not has_pending_voice:
                message = self._build_merged_private_message(messages)
                message._wxbot_recovery_turn_id = str((job or {}).get("turn_id", "") or "")
                message._wxbot_reply_turn_id = message._wxbot_recovery_turn_id
                message._wxbot_recovery_route_source = str(
                    (job or {}).get("route_source", "") or ""
                )
                message._wxbot_startup_recovery = True
                messages = [message]
            for message in messages:
                self._ui_ingress_queue.put((ConversationRef(conversation, chat_type), message))
                recovered += 1
        if recovered:
            log(level="WARNING", message=f"消息恢复：已重新排队 {recovered} 个未完成会话")
        return recovered

    def _persist_ui_message(self, conversation, message):
        if self.is_stop_requested():
            return False
        if not isinstance(conversation, ConversationRef) or not isinstance(message, MessageEnvelope):
            raise TypeError("微信回调只能提交纯数据消息")
        coordinator = getattr(self, "_inbound_coordinator", None)
        if coordinator is None:
            raise RuntimeError("消息事实库尚未初始化")
        event = self._build_inbound_event(conversation, message)
        accepted = coordinator.accept(event)
        if event.related_delivery_id:
            self._reply_echo_tracker.acknowledge(event.related_delivery_id)
        message._wxbot_event_id = accepted.event_id
        message._wxbot_event_ids = (accepted.event_id,)
        message._wxbot_event_version = accepted.version
        message._wxbot_reply_expires_at = (
            accepted.event.received_at + DEFAULT_REPLY_TTL_SECONDS
            if accepted.direction == "friend"
            else 0.0
        )
        message._wxbot_inbound_direction = accepted.direction
        message._wxbot_persisted = True
        message._wxbot_should_dispatch = bool(
            accepted.is_new and accepted.direction != "bot_echo"
        )
        return bool(accepted.is_new)

    def _enrich_persisted_ui_message(self, _conversation, message):
        event_id = str(getattr(message, "_wxbot_event_id", "") or "").strip()
        store = getattr(self, "_message_store", None)
        if not event_id or store is None:
            return False
        message_type = str(getattr(message, "type", "") or "text").strip().lower()
        content = str(getattr(message, "content", "") or "")
        stored_content = "[图片]" if message_type == "image" else content
        metadata = {}
        image_paths = self._extract_message_image_paths(message)
        if image_paths:
            metadata["image_paths"] = image_paths
        try:
            return store.update_inbound_content(
                event_id,
                stored_content,
                original_content=str(getattr(message, "original_content", "") or content),
                metadata=metadata,
            )
        except MessageStoreTransitionError:
            return False

    def _dispatch_persisted_ui_message(self, conversation, message):
        if self.is_stop_requested():
            return False
        if not bool(getattr(message, "_wxbot_persisted", False)):
            raise RuntimeError("消息尚未写入事实库")
        if not bool(getattr(message, "_wxbot_should_dispatch", False)):
            return True
        if bool(getattr(message, "_wxbot_dispatched", False)):
            return True
        message._wxbot_dispatched = True
        self._ui_ingress_queue.put((conversation, message))
        return True

    def _enqueue_ui_message(self, conversation, message):
        if not bool(getattr(message, "_wxbot_persisted", False)):
            self._persist_ui_message(conversation, message)
        return self._dispatch_persisted_ui_message(conversation, message)

    def _run_ui_ingress(self):
        while not self._ui_ingress_stop.is_set():
            ready = getattr(self, "_ui_ingress_ready", None)
            if ready is not None and not ready.wait(timeout=0.2):
                continue
            try:
                conversation, message = self._ui_ingress_queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                if not self._ui_owner.wait_for_contact_idle():
                    continue
                if conversation.chat_type == "group":
                    self._enqueue_group_message_for_business(conversation, message)
                else:
                    chat = OwnedChat(self._ui_owner, conversation.who, conversation.chat_type)
                    self.message_handle_callback(message, chat)
            except Exception as exc:
                retry_delay = self._reply_retry_delay(message, exc)
                if retry_delay is not None:
                    log(level="WARNING", message=f"消息库暂时繁忙，稍后重试：{exc}")
                    self._schedule_ui_ingress_retry(conversation, message, retry_delay)
                else:
                    cleanup_error = self._cancel_failed_reply_attempt(message, str(exc))
                    suffix = f"；取消失败：{cleanup_error}" if cleanup_error else ""
                    log(
                        level="ERROR",
                        message=f"微信消息业务队列处理失败：{exc}{suffix}\n{traceback.format_exc()}",
                    )
            finally:
                self._ui_ingress_queue.task_done()

    def _enqueue_group_message_for_business(self, conversation, message):
        self._ensure_message_runtime_state()
        if not isinstance(conversation, ConversationRef) or conversation.chat_type != "group":
            raise ValueError("group business queue requires a group ConversationRef")
        if self.is_stop_requested():
            self._cancel_failed_reply_attempt(message, "robot stopped before group message processing")
            return True
        with self._chat_merge_lock:
            pipeline = self._group_message_pipelines.get(conversation.who)
            if pipeline is None:
                pipeline = {
                    "conversation": conversation,
                    "messages": deque(),
                    "worker_running": False,
                    "retry_timer": None,
                }
                self._group_message_pipelines[conversation.who] = pipeline
            pipeline["messages"].append(message)
            self._start_group_message_worker_locked(pipeline)
        return True

    def _start_group_message_worker_locked(self, pipeline):
        if pipeline.get("worker_running") or pipeline.get("retry_timer") is not None:
            return
        pipeline["worker_running"] = True
        conversation = pipeline["conversation"]
        threading.Thread(
            target=self._run_group_message_pipeline_worker,
            args=(conversation,),
            name=f"group-message-{conversation.who}",
            daemon=True,
        ).start()

    def _run_group_message_pipeline_worker(self, conversation):
        while not self.is_stop_requested():
            with self._chat_merge_lock:
                pipeline = self._group_message_pipelines.get(conversation.who)
                if not pipeline or not pipeline["messages"]:
                    self._group_message_pipelines.pop(conversation.who, None)
                    return True
                message = pipeline["messages"][0]
            chat = OwnedChat(self._ui_owner, conversation.who, "group")
            try:
                self.message_handle_callback(message, chat)
            except Exception as exc:
                retry_delay = self._reply_retry_delay(message, exc)
                if retry_delay is not None:
                    log(level="WARNING", message=f"群聊消息库暂时繁忙，稍后重试：{exc}")
                    with self._chat_merge_lock:
                        pipeline = self._group_message_pipelines.get(conversation.who)
                        if pipeline:
                            pipeline["worker_running"] = False
                            timer = threading.Timer(
                                retry_delay,
                                self._resume_group_message_pipeline,
                                args=(conversation,),
                            )
                            timer.daemon = True
                            pipeline["retry_timer"] = timer
                            timer.start()
                    return True
                cleanup_error = self._cancel_failed_reply_attempt(message, str(exc))
                suffix = f"；取消失败：{cleanup_error}" if cleanup_error else ""
                log(
                    level="ERROR",
                    message=f"群聊消息处理失败：{exc}{suffix}\n{traceback.format_exc()}",
                )
            with self._chat_merge_lock:
                pipeline = self._group_message_pipelines.get(conversation.who)
                if pipeline and pipeline["messages"] and pipeline["messages"][0] is message:
                    if self._is_unresolved_pending_voice_message(message):
                        pipeline["worker_running"] = False
                        return True
                    pipeline["messages"].popleft()
        return True

    def _resume_group_message_pipeline(self, conversation):
        with self._chat_merge_lock:
            pipeline = self._group_message_pipelines.get(conversation.who)
            if not pipeline:
                return
            pipeline["retry_timer"] = None
            if self.is_stop_requested():
                self._group_message_pipelines.pop(conversation.who, None)
                return
            self._start_group_message_worker_locked(pipeline)

    def _clear_group_message_pipelines(self):
        self._ensure_message_runtime_state()
        with self._chat_merge_lock:
            timers = [
                pipeline.get("retry_timer")
                for pipeline in self._group_message_pipelines.values()
                if pipeline.get("retry_timer") is not None
            ]
            self._group_message_pipelines.clear()
        for timer in timers:
            timer.cancel()

    def _schedule_ui_ingress_retry(self, conversation, message, delay):
        def requeue():
            if not self.is_stop_requested():
                self._ui_ingress_queue.put((conversation, message))

        timer = threading.Timer(max(0.0, float(delay or 0.0)), requeue)
        timer.daemon = True
        timer.start()

    def _handle_ui_owner_timeout(self, snapshot):
        log(
            level="ERROR",
            message=(
                f"微信 UI 调用超过截止线：{snapshot.kind}，"
                f"开始 {snapshot.started_at:.3f}，截止 {snapshot.deadline_at:.3f}"
            ),
        )
        for thread_id, frame in sys._current_frames().items():
            stack = "".join(traceback.format_stack(frame))
            log(level="ERROR", message=f"线程 {thread_id} 堆栈：\n{stack}")
        owner = getattr(self, "_ui_owner", None)
        terminate_contact = getattr(owner, "terminate_active_contact_job", None)
        if callable(terminate_contact):
            try:
                terminate_contact()
            except Exception as exc:
                log(level="ERROR", message=f"卡死退出前终止通讯录采集器失败：{exc}")
        if self.is_stop_requested() or snapshot.kind == wechat_ui_actions.UIIntentKind.SHUTDOWN.value:
            try:
                runtime_dir = os.path.abspath("runtime")
                os.makedirs(runtime_dir, exist_ok=True)
                with open(os.path.join(runtime_dir, "suppress_scheduled_bot_start_until.txt"), "w", encoding="utf-8") as handle:
                    handle.write(str(time.time() + 300))
            except Exception as exc:
                log(level="ERROR", message=f"写入用户停止恢复标记失败：{exc}")
        os._exit(wechat_ui_actions.UI_STUCK_EXIT_CODE)

    def _activate_reply_echoes_for_ui_intent(self, intent):
        tracker = getattr(self, "_reply_echo_tracker", None)
        if tracker is not None:
            tracker.activate(intent.payload.get("echo_delivery_ids") or ())

    def _complete_reply_echoes_for_ui_intent(self, intent):
        tracker = getattr(self, "_reply_echo_tracker", None)
        if tracker is not None:
            tracker.complete(intent.payload.get("echo_delivery_ids") or ())

    def _bootstrap_ui_owner(self, listeners):
        if self._ui_owner is not None:
            return dict(self._ui_identity)
        orphan_cleanup = contacts.cleanup_orphaned_contact_auto_collector()
        if orphan_cleanup.get("terminated"):
            log(level="WARNING", message=f"已清理遗留通讯录采集进程 PID {orphan_cleanup.get('pid')}")
        self._ui_ingress_stop.clear()
        ready = getattr(self, "_ui_ingress_ready", None)
        if ready is None:
            ready = threading.Event()
            self._ui_ingress_ready = ready
        ready.clear()
        self._ui_ingress_thread = threading.Thread(
            target=self._run_ui_ingress,
            name="wechat-message-business",
            daemon=True,
        )
        self._ui_ingress_thread.start()
        self._ui_runtime = WeChatUIRuntime(
            self._enqueue_ui_message,
            inbound_media_enabled=lambda conversation, _message_type: bool(
                getattr(
                    self.config,
                    "group_image_recognition_switch"
                    if str(getattr(conversation, "chat_type", "private") or "private").lower() == "group"
                    else "chat_image_recognition_switch",
                    False,
                )
            ),
            persist_message=self._persist_ui_message,
            enrich_message=self._enrich_persisted_ui_message,
            echo_action_start=lambda action_id: self._reply_echo_tracker.activate((action_id,)),
            echo_action_finish=lambda action_id: self._reply_echo_tracker.complete((action_id,)),
        )
        self._ui_owner = wechat_ui_actions.WeChatUIOwner(
            self._ui_runtime.handlers(),
            conversation_version_provider=self._get_private_message_sequence,
            task_version_provider=self._current_ui_task_version,
            payload_preparer=self._prepare_ui_intent_payload,
            intent_start_callback=self._activate_reply_echoes_for_ui_intent,
            intent_finish_callback=self._complete_reply_echoes_for_ui_intent,
            runtime_id=self._runtime_instance_id,
        )
        self._ui_owner.start()
        self._ui_runtime.set_owner(self._ui_owner)
        self._ui_runtime.set_heartbeat(self._ui_owner.heartbeat_current_action)
        self._ui_watchdog = wechat_ui_actions.UIWatchdog(
            self._ui_owner.current_action_snapshot,
            self._handle_ui_owner_timeout,
        )
        self._ui_watchdog.start()
        identity = self._ui_owner.call(
            wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.BOOTSTRAP,
                {"listeners": []},
            ),
            wechat_ui_actions.UI_CALL_WAIT_TIMEOUT,
        )
        self._ui_identity = dict(identity or {})
        self.wx = UIClientFacade(self._ui_owner, self._ui_identity)
        return dict(self._ui_identity)

    def _prepare_ui_intent_payload(self, intent):
        payload = dict(intent.payload)
        target_contacts = list(payload.get("target_contacts") or [])
        if target_contacts:
            directory, _directory_file, _wx_id = self._load_contact_profiles_directory()
            subjects = [item for item in (directory.get("subjects") or []) if isinstance(item, dict)]
            resolved_targets = []
            for record in target_contacts:
                record = dict(record or {})
                contact_key = str(record.get("contact_key") or "").strip()
                if not contact_key:
                    if record.get("require_contact_key"):
                        raise wechat_ui_actions.IntentCancelled("联系人身份缺失或同名，已拒绝按昵称猜测")
                    send_name = str(record.get("send_name") or "").strip()
                    if send_name:
                        resolved_targets.append(send_name)
                    continue
                matches = [
                    contact for contact in subjects
                    if str(contact.get("contact_key") or "").strip() == contact_key
                ]
                if len(matches) != 1:
                    raise wechat_ui_actions.IntentCancelled("联系人已不存在或无法唯一定位")
                target = contact_send_target(matches[0])
                if not target:
                    raise wechat_ui_actions.IntentCancelled("联系人当前没有可用发送名")
                if sum(1 for contact in subjects if contact_send_target(contact) == target) != 1:
                    raise wechat_ui_actions.IntentCancelled("联系人当前发送名重复，已拒绝猜测")
                resolved_targets.append(target)
            payload["targets"] = resolved_targets
            return payload
        contact_key = str(payload.get("contact_key") or "").strip()
        if not contact_key:
            if payload.get("require_contact_key"):
                raise wechat_ui_actions.IntentCancelled("联系人身份缺失或同名，已拒绝按昵称猜测")
            return payload
        directory, _directory_file, _wx_id = self._load_contact_profiles_directory()
        matches = [
            contact for contact in (directory.get("subjects") or [])
            if isinstance(contact, dict) and str(contact.get("contact_key") or "").strip() == contact_key
        ]
        if len(matches) != 1:
            raise wechat_ui_actions.IntentCancelled("联系人已不存在或无法唯一定位")
        target = contact_send_target(matches[0])
        if not target:
            raise wechat_ui_actions.IntentCancelled("联系人当前没有可用发送名")
        subjects = [item for item in (directory.get("subjects") or []) if isinstance(item, dict)]
        if sum(1 for contact in subjects if contact_send_target(contact) == target) != 1:
            raise wechat_ui_actions.IntentCancelled("联系人当前发送名重复，已拒绝猜测")
        if "target" in payload:
            payload["target"] = target
        else:
            payload["conversation"] = target
        return payload

    def _current_ui_task_version(self, task_key):
        category, separator, task_id = str(task_key or "").partition(":")
        if not separator or not task_id:
            return 0
        if category == "scheduled_message":
            storage = self._scheduled_message_storage()
            tasks = storage.load_tasks() if storage is not None else None
            if tasks is None:
                tasks = getattr(self.config, "scheduled_message_task_list", []) or []
            for task in tasks:
                if not isinstance(task, dict) or str(task.get("id") or "").strip() != task_id:
                    continue
                definition, _runtime, _history = split_scheduled_message_task_storage(task)
                return wechat_ui_actions.task_definition_version(definition)
        if category == "material_outreach":
            storage = self._material_outreach_storage()
            tasks = storage.load_tasks() if storage is not None else None
            if tasks is None:
                tasks = getattr(self.config, "material_outreach_list", []) or []
            for task in tasks:
                current_id = str(task.get("id") or task.get("task_id") or "").strip() if isinstance(task, dict) else ""
                if current_id == task_id:
                    return wechat_ui_actions.task_definition_version(task)
        if category in {"contact_auto", "new_friend", "keyword", "group_welcome"}:
            definition = self._config_ui_task_definition(category)
            return wechat_ui_actions.task_definition_version(definition) if definition else 0
        if category == "relationship_auto":
            state = relationship_scan.load_bot_state(self)
            settings = relationship_scan.normalize_settings(state.get("settings"))
            return wechat_ui_actions.task_definition_version(settings)
        if category == "friend_request":
            state = friend_request.load_state(self.config.DATA_DIR, getattr(self, "wx_id", "") or "default")
            candidate = next((
                item for item in (state.get("candidates") or [])
                if isinstance(item, dict) and str(item.get("candidate_id") or "").strip() == task_id
            ), None)
            if candidate is None:
                return 0
            return friend_request.ui_guard(state, candidate)[1]
        return 0

    def _config_ui_task_definition(self, category):
        config_data = None
        config_file = str(getattr(self.config, "CONFIG_FILE", "") or "").strip()
        if config_file:
            try:
                with open(config_file, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    config_data = loaded
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                config_data = None
        if config_data is None:
            config_data = dict(getattr(self.config, "config", {}) or {})
        fields = {
            "contact_auto": (
                "contact_directory_auto_maintenance_switch",
                "contact_directory_auto_maintenance_interval_minutes",
                "contact_directory_auto_maintenance_window_start",
                "contact_directory_auto_maintenance_window_end",
            ),
            "new_friend": (
                "new_friend_switch",
                "new_friend_archive_switch",
                "new_friend_remark_prefix",
                "new_friend_remark_prefix_timestamp",
                "new_friend_remark_suffix",
                "new_friend_remark_suffix_timestamp",
                "new_friend_tags",
                "new_friend_reply_switch",
                "new_friend_msg",
            ),
            "keyword": (
                "chat_keyword_switch",
                "group_keyword_switch",
                "group_keyword_at_only",
                "keyword_dict",
            ),
            "group_welcome": (
                "group_welcome",
                "group_welcome_msg",
                "group",
            ),
        }.get(category, ())
        definition = {field: config_data.get(field) for field in fields}
        if category == "keyword":
            field = "keyword_dict"
            fallback = {}
            path_provider = getattr(self.config, "_keyword_rules_file", None)
            path = path_provider() if callable(path_provider) else None
            if path:
                try:
                    with open(path, "r", encoding="utf-8-sig") as handle:
                        loaded = json.load(handle)
                    definition[field] = loaded if isinstance(loaded, dict) else fallback
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    definition[field] = fallback
        return definition

    def _config_ui_task_guard(self, category):
        definition = self._config_ui_task_definition(category)
        if not definition:
            return "", 0
        return f"{category}:config", wechat_ui_actions.task_definition_version(definition)

    def _relationship_auto_ui_guard(self, settings=None):
        settings = relationship_scan.normalize_settings(settings)
        return "relationship_auto:settings", wechat_ui_actions.task_definition_version(settings)

    def _scheduled_message_ui_guard(self, task):
        task_id = str((task or {}).get("id") or "").strip()
        if not task_id:
            return "", 0
        task_key = f"scheduled_message:{task_id}"
        definition, _runtime, _history = split_scheduled_message_task_storage(task)
        return task_key, wechat_ui_actions.task_definition_version(definition)

    def _material_outreach_ui_guard(self, task):
        task_id = str((task or {}).get("id") or (task or {}).get("task_id") or "").strip()
        if not task_id:
            return "", 0
        task_key = f"material_outreach:{task_id}"
        current_version = self._current_ui_task_version(task_key)
        return task_key, current_version or wechat_ui_actions.task_definition_version(task)

    @staticmethod
    def _ui_message_payload(chat, message):
        return {
            "conversation": str(getattr(chat, "who", "") or ""),
            "chat_type": str(getattr(chat, "chat_type", "private") or "private"),
            "message_type": str(getattr(message, "type", "") or ""),
            "message_attr": str(getattr(message, "attr", "") or ""),
            "message_sender": str(getattr(message, "sender", "") or ""),
            "message_content": str(getattr(message, "original_content", "") or getattr(message, "content", "") or ""),
            "message_id": getattr(message, "id", ""),
            "message_hash": getattr(message, "hash", ""),
            "message_hash_text": getattr(message, "hash_text", ""),
            "message_window_order": int(getattr(message, "window_order", 0) or 0),
            "message_window_order_known": str(getattr(message, "_wxbot_ingress_source", "") or "") in {
                "window_snapshot", "voice_snapshot", "material_history", "global"
            },
        }

    def _ui_download_message(self, chat, message, *, quote_image=False):
        payload = self._ui_message_payload(chat, message)
        payload["quote_image"] = bool(quote_image)
        payload["allow_history_fallback"] = False
        return self._ui_owner.call(
            wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.DOWNLOAD_MEDIA, payload),
            wechat_ui_actions.UI_CALL_WAIT_TIMEOUT,
        )

    def _ui_forward_message(
        self,
        chat,
        message,
        targets,
        *,
        preface="",
        task_key="",
        task_version=0,
        require_contact_key=False,
        delivery_id="",
        request_id="",
        run_id="",
        batch_id="",
        echo_delivery_ids=(),
    ):
        payload = self._ui_message_payload(chat, message)
        context = getattr(getattr(self, "_material_ui_context", None), "value", None) or {}
        task_key = str(task_key or context.get("task_key") or "")
        task_version = int(task_version or context.get("task_version") or 0)
        contacts_by_name = dict(context.get("contacts_by_name") or {})
        target_contacts = []
        for target in targets if isinstance(targets, (list, tuple)) else [targets]:
            name = str(target or "").strip()
            record = dict(contacts_by_name.get(name) or {})
            target_contacts.append({
                "contact_key": str(record.get("contact_key") or ""),
                "send_name": name,
                "require_contact_key": bool(record.get("require_contact_key")),
            })
        payload.update({
            "targets": targets,
            "target_contacts": target_contacts,
            "preface": str(preface or ""),
            "delivery_id": str(delivery_id or uuid.uuid4()),
            "request_id": str(request_id or ""),
            "run_id": str(run_id or ""),
            "batch_id": str(batch_id or ""),
            "task_key": task_key,
            "echo_delivery_ids": list(echo_delivery_ids or ()),
        })
        return self._ui_owner.call(
            wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.FORWARD,
                payload,
                task_version=task_version,
            ),
            wechat_ui_actions.UI_CALL_WAIT_TIMEOUT,
        )

    def _ui_quote_message(
        self,
        chat,
        message,
        text,
        *,
        at="",
        journal=True,
        conversation_version=None,
        echo_delivery_ids=(),
        expires_at=None,
    ):
        payload = self._ui_message_payload(chat, message)
        payload.update({
            "text": str(text or ""),
            "at": str(at or ""),
            "delivery_id": str(uuid.uuid4()) if journal else "",
            "echo_delivery_ids": list(echo_delivery_ids or ()),
        })
        return self._ui_owner.call(
            wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.QUOTE,
                payload,
                conversation_version=conversation_version,
                expires_at=expires_at,
            ),
            wechat_ui_actions.UI_CALL_WAIT_TIMEOUT,
        )

    def _run_contact_auto_maintenance_collector(
        self,
        *,
        start_name,
        start_identity="",
        count,
        timeout_seconds,
        run_kind="auto_maintenance",
    ):
        task_key, task_version = (
            self._config_ui_task_guard("contact_auto")
            if run_kind == "auto_maintenance"
            else ("", 0)
        )
        return self._ui_owner.call(
            wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.CONTACT_START,
                {
                    "start_name": str(start_name or ""),
                    "start_identity": str(start_identity or ""),
                    "count": 50,
                    "task_key": task_key,
                },
                task_version=task_version,
            ),
            310,
        )

    def _ensure_stop_requested_event(self):
        event = getattr(self, "_stop_requested", None)
        if event is None or not hasattr(event, "is_set"):
            event = threading.Event()
            self._stop_requested = event
        return event

    def _start_wxauto_save_cache_cleanup(self):
        def run_cleanup():
            try:
                retention_days = int(getattr(self.config, "wxauto_save_cache_retention_days", 30) or 0)
                if retention_days <= 0:
                    return
                stats = cleanup_wxauto_save_cache(WxParam.DEFAULT_SAVE_PATH, retention_days=retention_days)
                if not stats or stats.get("skipped"):
                    return
                deleted_files = int(stats.get("deleted_files") or 0)
                deleted_dirs = int(stats.get("deleted_dirs") or 0)
                failed = int(stats.get("failed") or 0)
                if deleted_files or deleted_dirs or failed:
                    log(
                        "INFO",
                        "wxauto_save 缓存清理完成："
                        f"删除文件 {deleted_files} 个，清理空目录 {deleted_dirs} 个，失败 {failed} 个",
                    )
            except Exception as exc:
                log("WARNING", f"wxauto_save 缓存清理失败（已跳过，不影响启动）：{exc}")

        threading.Thread(target=run_cleanup, name="wxauto-save-cache-cleanup", daemon=True).start()

    def _reset_stop_request(self):
        lock = getattr(self, "_stop_cleanup_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._stop_cleanup_lock = lock
        with lock:
            self._ensure_stop_requested_event().clear()
            self._stop_cleanup_done = False

    def is_stop_requested(self):
        return self._ensure_stop_requested_event().is_set()

    def _wait_or_stop_requested(self, seconds):
        seconds = max(0.0, float(seconds or 0))
        if seconds <= 0:
            return self.is_stop_requested()
        return self._ensure_stop_requested_event().wait(seconds)

    def _cancel_pending_private_message_timers(self):
        self._ensure_message_runtime_state()
        with self._chat_merge_lock:
            timers = []
            for pipeline in self._private_message_pipelines.values():
                if not isinstance(pipeline, dict):
                    continue
                for key in ("idle_timer", "max_timer"):
                    timer = pipeline.get(key)
                    if timer:
                        timers.append(timer)
                pipeline["open_messages"] = []
                pipeline["queued_batches"] = deque()
                pipeline["idle_timer"] = None
                pipeline["max_timer"] = None
            pending_voice = getattr(self, "_pending_private_voice_transcription", None)
            if isinstance(pending_voice, dict):
                for task in pending_voice.values():
                    if isinstance(task, dict) and task.get("timer"):
                        timers.append(task.get("timer"))
                pending_voice.clear()
            for pipeline in self._private_message_pipelines.values():
                if not isinstance(pipeline, dict):
                    continue
                pipeline["worker_running"] = False
        for timer in timers:
            try:
                timer.cancel()
            except Exception:
                pass

    def _runtime_metrics_path(self):
        data_dir = str(getattr(getattr(self, "config", None), "DATA_DIR", getattr(self, "DATA_DIR", "")) or "").strip()
        if not data_dir:
            _base = os.path.dirname(sys.executable) if hasattr(sys, '_MEIPASS') else os.path.abspath(".")
            data_dir = os.path.join(_base, 'data')
        return os.path.join(data_dir, 'config', 'runtime_metrics_v1.json')

    def _runtime_metrics_store(self):
        store = getattr(self, "_runtime_metrics_store_instance", None)
        path = self._runtime_metrics_path()
        if isinstance(store, RuntimeMetricsStore) and str(getattr(store, "path", "")) == str(path):
            return store
        store = RuntimeMetricsStore(path)
        self._runtime_metrics_store_instance = store
        return store

    def get_runtime_metrics_series(self, now=None, days=7):
        try:
            return self._runtime_metrics_store().series_payload(now=now, days=days)
        except Exception:
            return {"status": "success", "updated_at": "", "range_days": days, "hourly": [], "daily": [], "today": {}}

    def _metric_increment(self, key, amount=1, now=None):
        try:
            self._runtime_metrics_store().increment(key, amount=amount, now=now)
        except Exception:
            pass

    def _metric_add_unique(self, key, identity, now=None):
        try:
            self._runtime_metrics_store().add_unique(key, identity, now=now)
        except Exception:
            pass

    def _metric_record_active_chat(self, chat_name, *, chat_type="private", now=None):
        key = "active_group_chats" if str(chat_type or "").strip() == "group" else "active_private_chats"
        self._metric_add_unique(key, chat_name, now=now)

    def _metric_set_today_relationship_counts(self, *, blocked=0, deleted=0, now=None):
        try:
            self._runtime_metrics_store().set_today_counts(
                {
                    "relationship_blocked_today": blocked,
                    "relationship_deleted_today": deleted,
                },
                now=now,
            )
        except Exception:
            pass

    def runtime_metrics_today(self, now=None):
        try:
            return (self._runtime_metrics_store().series_payload(now=now, days=1).get("today") or {})
        except Exception:
            return {}

    def _record_received_message(self):
        self.msg_received_count = int(getattr(self, "msg_received_count", 0) or 0) + 1
        self._metric_increment("received_messages")

    def _record_replied_message_success(self, chat_name="", chat_type="private"):
        self.msg_replied_count = int(getattr(self, "msg_replied_count", 0) or 0) + 1
        self._metric_increment("reply_count")
        if chat_name:
            self._metric_record_active_chat(chat_name, chat_type=chat_type)

    def _record_reply_metric_success(self, chat_name="", chat_type="private"):
        self._record_replied_message_success(chat_name, chat_type=chat_type)

    def _record_keyword_reply_success(self, chat_name="", chat_type="private", action_count=1):
        try:
            count = int(action_count or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            count = 1
        self._metric_increment("keyword_reply_triggers")
        self._metric_increment("keyword_reply_messages", amount=count)
        if chat_name:
            set_key = "keyword_group_targets" if str(chat_type or "").strip() == "group" else "keyword_private_targets"
            self._metric_add_unique(set_key, chat_name)

    def _record_scheduled_message_send_successes(self, count, *, trigger_kind="fixed"):
        try:
            count = int(count or 0)
        except (TypeError, ValueError):
            count = 0
        kind_text = str(trigger_kind or "").strip()
        kind = "random" if "random" in kind_text else "fixed"
        self._metric_increment(f"scheduled_{kind}_runs")
        if count > 0:
            self._metric_increment(f"scheduled_{kind}_success_targets", amount=count)

    def _record_material_outreach_send_success(self, task_id, target=""):
        if str(task_id or "").strip() == AI_AUTO_OUTREACH_TASK_ID:
            self._metric_increment("ai_material_success_count")
            if target:
                self._metric_add_unique("ai_material_success_targets", target)
        else:
            self._metric_increment("material_success_count")
            if target:
                self._metric_add_unique("material_success_targets", target)

    def _record_chat_api_request(self):
        self._metric_increment("api_calls")
        self._metric_increment("chat_api_calls")

    def _record_image_api_request(self):
        self._metric_increment("image_api_calls")

    def _record_material_preface_api_request(self):
        self._metric_increment("material_preface_api_calls")

    def _record_ai_outreach_decision_api_request(self):
        self._metric_increment("ai_outreach_decision_api_calls")

    def _record_ai_outreach_preface_api_request(self):
        self._metric_increment("ai_outreach_preface_api_calls")

    def _record_tts_api_request(self):
        self._metric_increment("api_calls")

    def _record_other_api_request(self):
        self._metric_increment("api_calls")

    def _wrap_api_request_counter(self, api, request_type):
        stat_key = str(request_type or "").strip()
        if stat_key == "chat":
            return _CountingAPIProxy(api, self._record_chat_api_request)
        if stat_key == "other":
            return _CountingAPIProxy(api, self._record_other_api_request)
        return api

    def _material_outreach_tasks_file(self, *, wx_id=None, create_parent=False):
        return self._material_outreach_store(wx_id=wx_id).tasks_file(create_parent=create_parent)

    def _material_outreach_runtime_file(self, *, wx_id=None, create_parent=False):
        return self._material_outreach_store(wx_id=wx_id).runtime_file(create_parent=create_parent)

    def _material_outreach_history_file(self, *, wx_id=None, create_parent=False):
        return self._material_outreach_store(wx_id=wx_id).history_file(create_parent=create_parent)

    def _material_outreach_materials_file(self, *, wx_id=None, create_parent=False):
        return self._material_outreach_store(wx_id=wx_id).materials_file(create_parent=create_parent)

    def _material_outreach_store(self, *, wx_id=None):
        return MaterialOutreachStorage(
            str(getattr(self.config, "DATA_DIR", "") or "").strip(),
            resolve_account_id(
                wx_id
                or getattr(self.config, "current_account_wx_id", "")
                or getattr(self, "current_account_wx_id", "")
                or getattr(self, "wx_id", ""),
                fallback_default=True,
            ),
        )

    def _load_material_outreach_runtime(self):
        return self._material_outreach_store().load_runtime()

    def _save_material_outreach_runtime(self, payload):
        return self._material_outreach_store().save_runtime(payload if isinstance(payload, dict) else {})

    def _load_material_outreach_history(self):
        return self._material_outreach_store().load_history()

    def _save_material_outreach_history(self, payload):
        return self._material_outreach_store().save_history(payload)

    def _load_material_outreach_materials(self):
        return self._material_outreach_store().load_materials()

    def _save_material_outreach_materials(self, materials):
        return self._material_outreach_store().save_materials(materials)

    def _load_material_send_records(self):
        return self._material_outreach_store().load_send_records()

    def _save_material_send_records(self, records):
        return self._material_outreach_store().save_send_records(records)

    def _append_material_send_record(self, record, *, limit=1000):
        records = self._material_outreach_store().append_send_record(record, limit=limit)
        if isinstance(record, dict) and record.get("success"):
            self._record_material_outreach_send_success(record.get("task_id"), record.get("target"))
        return records

    def _load_material_skip_records(self):
        return self._material_outreach_store().load_skip_records()

    def _save_material_skip_records(self, records):
        return self._material_outreach_store().save_skip_records(records)

    def _append_material_skip_record(self, record, *, limit=1000):
        return self._material_outreach_store().append_skip_record(record, limit=limit)

    def _load_material_progress_records(self):
        return self._material_outreach_store().load_progress_records()

    def _save_material_progress_records(self, records):
        return self._material_outreach_store().save_progress_records(records)

    def _append_material_progress_records(self, records, *, limit=1000):
        return self._material_outreach_store().append_progress_records(records, limit=limit)

    def _update_material_progress_records_for_send(self, snapshot, targets, *, success=False, status="", error="", now=None, limit=1000):
        return self._material_outreach_store().update_progress_records_for_send(
            snapshot,
            targets,
            success=success,
            status=status,
            error=error,
            now=now,
            limit=limit,
        )

    def _load_ai_pending_queue(self):
        return self._load_material_outreach_runtime().get("ai_pending_queue", [])

    def _save_ai_pending_queue(self, records):
        runtime = self._load_material_outreach_runtime()
        runtime["ai_pending_queue"] = [item for item in (records or []) if isinstance(item, dict)]
        self._save_material_outreach_runtime(runtime)
        return runtime["ai_pending_queue"]

    def _load_material_outreach_preface_queue(self):
        return normalize_preface_pending_queue(
            self._load_material_outreach_runtime().get("preface_pending_queue", [])
        )

    def _save_material_outreach_preface_queue(self, records):
        runtime = self._load_material_outreach_runtime()
        runtime["preface_pending_queue"] = normalize_preface_pending_queue(records)
        self._save_material_outreach_runtime(runtime)
        return runtime["preface_pending_queue"]

    def _set_material_outreach_namespace(self, wx_id=None):
        wx_id = str(wx_id or "").strip()
        account_wx_id = resolve_account_id(
            wx_id or getattr(self, "current_account_wx_id", ""),
            fallback_default=True,
        )
        account_module_dir(self.config.DATA_DIR, account_wx_id or "default", "material_outreach", create=True)
        if account_wx_id:
            self._save_material_outreach_materials(self._load_material_outreach_materials())
            self._save_material_outreach_history(self._load_material_outreach_history())
            self._save_material_outreach_runtime(self._load_material_outreach_runtime())
            recovered = self._material_outreach_store(wx_id=account_wx_id).freeze_all_interrupted_sends()
            if recovered:
                log(
                    level="WARNING",
                    message=f"素材转发：恢复 {len(recovered)} 条提交结果未知记录，已禁止自动重发",
                )



    def _get_material_source_read_lock(self, source):
        source = str(source or "").strip()
        if not source:
            source = "__default__"
        if not hasattr(self, "_material_source_read_locks") or self._material_source_read_locks is None:
            self._material_source_read_locks = {}
        if not hasattr(self, "_material_source_read_locks_guard") or self._material_source_read_locks_guard is None:
            self._material_source_read_locks_guard = threading.Lock()
        with self._material_source_read_locks_guard:
            lock = self._material_source_read_locks.get(source)
            if lock is None:
                lock = threading.RLock()
                self._material_source_read_locks[source] = lock
            return lock

    def _send_tracked_outbound(
        self,
        target,
        action,
        sender,
        *,
        source,
        chat_type="private",
        delivery_id="",
        at="",
    ):
        action = dict(action or {})
        kind = str(action.get("type") or "text").strip().lower()
        if kind in {"voice", "audio"}:
            reply_action = ReplyAction(ReplyKind.VOICE, "[语音]", ReplySource.AI)
            history_type = "voice"
        elif kind == "file":
            path = str(action.get("path") or "")
            reply_action = ReplyAction(ReplyKind.FILE, f"[文件] {os.path.basename(path)}".strip(), ReplySource.AI)
            history_type = "file"
        else:
            reply_action = ReplyAction(
                ReplyKind.TEXT,
                str(action.get("text") or action.get("content") or ""),
                ReplySource.AI,
            )
            history_type = "text"
        delivery_id = str(delivery_id or f"ui_{uuid.uuid4().hex}").strip()
        tracker = getattr(self, "_reply_echo_tracker", None)
        if tracker is not None:
            tracker.reserve(
                delivery_id,
                target,
                reply_action,
                confirmable=False,
                chat_type=chat_type,
                at=at,
            )
        try:
            result = sender(delivery_id)
        except wechat_ui_actions.IntentCancelled:
            if tracker is not None:
                tracker.discard(delivery_id)
            raise
        store = getattr(self, "_message_store", None)
        if ReplyCountStore.was_send_success(result) and store is not None:
            store.append_confirmed_outbound_once(
                delivery_id,
                target,
                content=reply_action.content,
                sent_at=time.time(),
                chat_type=chat_type,
                message_type=history_type,
                metadata={"source": str(source or "")},
            )
        return result

    def _send_outbound_to_target(
        self,
        target,
        action,
        *,
        contact_key="",
        task_key="",
        task_version=0,
        require_contact_key=False,
    ):
        target = str(target or "").strip()
        action = dict(action or {})
        kind = str(action.get("type") or "").strip().lower()
        if not target or getattr(self, "_ui_owner", None) is None:
            return False
        if kind == "text":
            value = str(action.get("text") or "")
            intent_kind = wechat_ui_actions.UIIntentKind.SEND_TEXT
            value_key = "text"
        elif kind == "file":
            value = str(action.get("path") or "").strip()
            if not value:
                return False
            intent_kind = wechat_ui_actions.UIIntentKind.SEND_FILE
            value_key = "path"
        else:
            raise ValueError(f"不支持的主动发送动作: {kind or 'empty'}")

        return self._send_tracked_outbound(
            target,
            {"type": kind, value_key: value},
            lambda delivery_id: self._ui_owner.call(
                wechat_ui_actions.UIIntent(
                    intent_kind,
                    {
                        "conversation": target,
                        "chat_type": "private",
                        "contact_key": str(contact_key or ""),
                        "task_key": str(task_key or ""),
                        "require_contact_key": bool(require_contact_key),
                        value_key: value,
                        "delivery_id": delivery_id if kind == "file" else "",
                        "echo_delivery_ids": [delivery_id],
                    },
                    task_version=task_version,
                ),
                wechat_ui_actions.UI_CALL_WAIT_TIMEOUT,
            ),
            source=kind,
        )

    def _prepare_contact_directory_window(self):
        return contacts.prepare_contact_directory_window(self)

    def _refresh_run_kind(self, mode: str, *, automatic: bool = False) -> str:
        return contacts.refresh_run_kind(mode, automatic=automatic)

    def _contact_directory_run_label(self, mode: str, *, run_kind: str = "") -> str:
        return contacts.contact_directory_run_label(mode, run_kind=run_kind)

    def _summarize_directory_growth(self, before_directory, after_directory) -> dict[str, int]:
        return contacts.summarize_directory_growth(before_directory, after_directory)

    def _init_prompt_system(self, state_dir=None):
        """初始化 Prompt 路由与会话记忆合成系统。"""
        if state_dir is None:
            _base = os.path.dirname(sys.executable) if hasattr(sys, '_MEIPASS') else os.path.abspath(".")
            wx_id = str(getattr(self, 'wx_id', '') or '').strip()
            state_dir = str(
                account_area_dir(
                    os.path.join(_base, 'data'),
                    wx_id,
                    'chat_memory',
                    create=True,
                    fallback_default=True,
                )
            )
        wx_id = str(getattr(self, 'wx_id', '') or '').strip()
        _base = os.path.dirname(sys.executable) if hasattr(sys, '_MEIPASS') else os.path.abspath(".")
        prompt_dir = os.path.join(_base, 'data', 'prompt')
        self.prompt_system = PromptSystem(
            self.config,
            state_dir=state_dir,
            prompt_dir=prompt_dir,
        )
        return self.prompt_system

    def _identity_base_dir(self):
        return os.path.join(
            os.path.dirname(sys.executable) if hasattr(sys, '_MEIPASS') else os.path.abspath("."),
            'data',
        )

    def _reconcile_identity_storage(self, old_chat_name, new_chat_name, *, reason=""):
        old_chat_name = str(old_chat_name or "").strip()
        new_chat_name = str(new_chat_name or "").strip()
        if not old_chat_name or not new_chat_name or old_chat_name == new_chat_name:
            return None
        wx_id = str(getattr(self, 'wx_id', '') or '').strip()
        if not wx_id:
            return None
        try:
            manifest = reconcile_contact_storage_names(
                self._identity_base_dir(),
                wx_id,
                old_chat_name,
                new_chat_name,
                reason=reason,
            )
            log(
                level="SUCCESS",
                message=f"身份校准：已将 {old_chat_name} 合并/改名到 {new_chat_name}",
            )
            refresh_config = getattr(getattr(self, "config", None), "refresh_config", None)
            if callable(refresh_config):
                refresh_config()
            self.reply_count_store = ReplyCountStore(os.path.join(self._identity_base_dir(), 'config', 'reply_count.json'))
            return manifest
        except Exception as exc:
            log(level="WARNING", message=f"身份校准：{old_chat_name} -> {new_chat_name} 失败：{exc}")
            return None

    def _sync_contact_identity_from_contact_directory(self, directory):
        wx_id = str(getattr(self, 'wx_id', '') or '').strip()
        if not wx_id:
            return None
        try:
            updated_directory, actions = sync_identity_calibration_from_directory(
                directory,
                wx_id=wx_id,
            )
            retry_actions = []
            for action in actions:
                if action.get("type") == "rename":
                    manifest = self._reconcile_identity_storage(
                        action.get("old_chat_name"),
                        action.get("new_chat_name"),
                        reason=action.get("reason", "contact_profiles"),
                    )
                    if not manifest:
                        retry_action = dict(action)
                        retry_action["last_error"] = "reconcile_failed"
                        retry_action["last_attempt_at"] = datetime.now().replace(microsecond=0).isoformat()
                        retry_actions.append(retry_action)
                else:
                    retry_actions.append(action)
            if retry_actions:
                identity_state = updated_directory.setdefault("identity_calibration", {})
                identity_state["actions"] = retry_actions
            self._save_contact_profiles_directory(updated_directory)
            return updated_directory
        except Exception as exc:
            log(level="WARNING", message=f"通讯录身份校准状态更新失败：{exc}")
            return None

    def _build_prompt_with_context(
        self,
        chat_name,
        *,
        base_prompt=None,
        chat_type='private',
        image_parse_block="",
        prompt_extra="",
    ):
        """在原 Prompt 上叠加会话记忆上下文；未启用时返回基础 Prompt。"""
        system = getattr(self, 'prompt_system', None)
        if system is None:
            system = self._init_prompt_system()
        return system.build_prompt(
            chat_name,
            base_prompt=base_prompt,
            chat_type=chat_type,
            image_parse_block=image_parse_block,
            prompt_extra=prompt_extra,
        )

    def _init_api(self):
        """根据当前选中的接口快照实例化默认 AI 接口对象。"""
        api_config = getattr(self.config, "current_api_config", None)
        if not isinstance(api_config, APIConfigSnapshot):
            api_configs = getattr(self.config, "api_configs", []) or []
            api_index = int(getattr(self.config, "api_index", 0) or 0) if api_configs else 0
            current = api_configs[api_index] if api_configs and 0 <= api_index < len(api_configs) else {}
            api_config = build_api_config_snapshot(
                current,
                prompt="",
                max_retries=getattr(self.config, "max_retries", 5),
                interface_index=api_index,
            )
            self.config.current_api_config = api_config
        sdk = api_config.sdk
        if sdk == "OpenAI SDK":
            log(level="DEBUG", message="聊天接口：OpenAI 已加载")
            return OpenAIAPI(api_config)
        elif sdk == "DusAPI":
            log(level="DEBUG", message="聊天接口：DusAPI 已加载")
            return DusAPI(api_config)
        else:
            raise ValueError(f"不支持的聊天接口 SDK：{sdk or '（空）'}")

    def _init_api_by_index(self, idx):
        """
        根据指定接口索引实例化 AI 接口对象。
        会创建一个只含接口相关字段的轻量代理配置对象，避免干扰主配置。
        """
        configs = self.config.api_configs
        if idx < 0 or idx >= len(configs):
            log(level="WARNING", message=f"接口索引 {idx} 超出范围，回退到默认接口")
            return self.api
        cfg = configs[idx]
        tmp = build_api_config_snapshot(
            cfg,
            prompt='',
            max_retries=getattr(self.config, "max_retries", 5),
            interface_index=idx,
        )
        sdk = tmp.sdk

        log(level="DEBUG", message=f"聊天接口已就绪：接口{idx + 1}，{sdk}，模型 {tmp.model}")
        if sdk == "OpenAI SDK":
            return OpenAIAPI(tmp)
        elif sdk == "DusAPI":
            return DusAPI(tmp)
        else:
            raise ValueError(f"不支持的聊天接口 SDK：{sdk or '（空）'}")

    # ----------------------------------------------------------
    # 初始化与检测
    # ----------------------------------------------------------

    def wxautox_activate_check(self):
        """检查 wxautox 授权是否已激活"""
        return check_license()

    def _notify_startup_status(self, success, message):
        callback = getattr(self, "_startup_callback", None)
        if callable(callback):
            try:
                callback(bool(success), str(message or ""))
            except Exception:
                pass

    def check_wechat_window(self):
        """检测微信客户端是否在线（未被弹出登录）"""
        return self.wx.IsOnline()

    def is_err(self, id, err="无"):
        """
        记录错误信息并发送告警通知。

        :param id:  错误标题（邮件主题）
        :param err: 错误详情（可为异常对象或字符串）
        """
        print(traceback.format_exc())
        log(level="ERROR", message=f"出现错误：{err}")
        content = '错误信息：\n' + traceback.format_exc() + "\nerr信息：\n" + str(err)
        try:
            email_send.send_email(subject=id, content=content)
        except Exception as email_err:
            log(level="ERROR", message=f"发送报错邮箱失败：{email_err}")

    def key_pass(self, year, month, day, hour, minute, second):
        """
        打包保护锁：检测程序是否已过期。
        若当前时间超过指定时间，则阻塞程序不可继续使用。
        """
        target_time  = datetime(year, month, day, hour, minute, second)
        current_time = datetime.now()

        if current_time < target_time:
            remaining_time = target_time - current_time
            days = remaining_time.days
            hours, remainder = divmod(remaining_time.seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            log(level="INFO", message=f"还剩 {days} 天 {hours} 小时 {minutes} 分钟 {seconds} 秒 到期。")
        else:
            # 已过期，永久阻塞
            while True:
                log(level="ERROR", message=f"程序以于 {target_time} 过期不可使用")
                time.sleep(60)

    # ----------------------------------------------------------
    # 微信监听器初始化
    # ----------------------------------------------------------

    def request_runtime_task_reload(self):
        """标记一次运行中任务热更新请求，由主循环在安全时机处理。"""
        with self._runtime_task_reload_lock:
            self._runtime_task_reload_requested = True

    def apply_runtime_api_config_update(self, merged_config):
        """将面板刚保存的接口配置同步到运行中实例，避免必须重启后才生效。"""
        if not isinstance(merged_config, dict):
            return
        for field in (
            'api_configs',
            'api_index',
            'api_capability_map',
            'backup_chat_api_index',
            'backup_chat_api_failover_threshold',
        ):
            if field in merged_config:
                self.config.config[field] = merged_config[field]
        self._sync_runtime_api_config_fields_from_config()
        self.api = self._init_api()
        self.api_cache = {}
        self._reset_chat_api_failover_state(active_index=self._get_primary_chat_api_index())
        log(message="运行中聊天接口配置已同步，主/备接口状态已重置为主接口")

    def _consume_runtime_task_reload_request(self):
        with self._runtime_task_reload_lock:
            requested = bool(getattr(self, '_runtime_task_reload_requested', False))
            if requested:
                self._runtime_task_reload_requested = False
            return requested

    def _reset_runtime_task_states(self):
        self._random_msg_state = {}
        self._random_material_outreach_state = {}

    def _register_runtime_task_schedules(self):
        log(level="DEBUG", message="启动阶段：定时任务扫描已就绪")

    def _task_storage_data_dir(self):
        config_data_dir = str(getattr(getattr(self, "config", None), "DATA_DIR", "") or "").strip()
        return config_data_dir or str(getattr(self, "DATA_DIR", "") or "").strip()

    def _task_storage_wx_id(self, wx_id=None):
        return resolve_account_id(
            wx_id
            or getattr(getattr(self, "config", None), "current_account_wx_id", "")
            or getattr(self, "current_account_wx_id", "")
            or getattr(self, "wx_id", ""),
            fallback_default=True,
        )

    def _scheduled_message_storage(self, *, wx_id=None):
        data_dir = self._task_storage_data_dir()
        if not data_dir:
            return None
        return TaskWorkbenchStorage(data_dir, self._task_storage_wx_id(wx_id), "scheduled_message")


    def _material_outreach_storage(self, *, wx_id=None):
        data_dir = self._task_storage_data_dir()
        if not data_dir:
            return None
        return TaskWorkbenchStorage(data_dir, self._task_storage_wx_id(wx_id), "material_outreach")

    def _material_outreach_runtime_lock(self):
        storage = self._material_outreach_storage()
        if storage is None:
            return nullcontext()
        return file_lock_for_path(storage.module_file("runtime.json", create_parent=True))

    def _set_runtime_task_list(self, field_name, tasks):
        setattr(self.config, field_name, tasks)
        config_map = getattr(self.config, "config", None)
        if isinstance(config_map, dict):
            config_map[field_name] = tasks

    def _save_scheduled_message_task_definitions_only(self, tasks):
        storage = self._scheduled_message_storage()
        if storage is None:
            return
        normalized = [
            normalize_scheduled_message_task_payload(task)
            for task in (tasks or [])
            if isinstance(task, dict)
        ]
        definitions, _runtime_map, _history_map = serialize_scheduled_message_task_collection(normalized)
        storage.save_tasks(definitions)

    def _save_scheduled_message_runtime_record(self, task):
        storage = self._scheduled_message_storage()
        if storage is None:
            return
        definition, runtime_record, _history = split_scheduled_message_task_storage(task)
        task_id = str(definition.get("id") or "").strip()
        if not task_id:
            return
        storage.mutate_runtime(
            lambda runtime_map: {
                **(runtime_map if isinstance(runtime_map, dict) else {}),
                task_id: runtime_record,
            }
        )

    def _save_scheduled_message_runtime_history_records(self, task):
        storage = self._scheduled_message_storage()
        if storage is None:
            return
        definition, runtime_record, history_record = split_scheduled_message_task_storage(task)
        task_id = str(definition.get("id") or "").strip()
        if not task_id:
            return
        storage.mutate_runtime(
            lambda runtime_map: {
                **(runtime_map if isinstance(runtime_map, dict) else {}),
                task_id: runtime_record,
            }
        )
        storage.mutate_history(
            lambda history_map: {
                **(history_map if isinstance(history_map, dict) else {}),
                task_id: history_record,
            }
        )

    def _save_material_outreach_task_definitions_only(self, tasks):
        storage = self._material_outreach_storage()
        if storage is None:
            return
        normalized = [
            normalize_material_outreach_task(task)
            for task in (tasks or [])
            if isinstance(task, dict)
        ]
        storage.save_tasks(normalized)




    def _compile_fixed_runtime_plan(self, task, *, default_time="08:00", now=None):
        task = normalize_fixed_task_schedule(task, default_time=default_time, start_at_key="start_at")
        plan = {
            "id": str(task.get("id") or task.get("task_id") or "").strip(),
            "schedule_mode": "fixed_at",
            "repeat_mode": str(task.get("repeat_mode") or "once").strip() or "once",
            "repeat_rule": str(task.get("repeat_rule") or "daily").strip() or "daily",
            "repeat_values": list(task.get("repeat_values") or []),
            "time_value": str(task.get("time_value") or default_time).strip() or default_time,
            "fire_at": str(task.get("fire_at") or "").strip(),
            "next_fire_at": str(task.get("next_fire_at") or "").strip(),
            "status": str(task.get("status") or "pending").strip() or "pending",
            "last_run_at": str(task.get("last_run_at") or "").strip(),
            "last_error": str(task.get("last_error") or "").strip(),
        }
        return compile_task_plan(plan, now=now)

    def _sync_runtime_plan_fields(self, task, plan):
        if not isinstance(task, dict) or not isinstance(plan, dict):
            return False
        changed = False
        for key in ("next_fire_at", "status", "last_run_at", "last_error"):
            value = plan.get(key, "")
            if task.get(key) != value:
                task[key] = value
                changed = True
        return changed

    def _load_random_runtime_state(self, task):
        task = task if isinstance(task, dict) else {}
        next_fire = None
        next_fire_text = str(task.get("next_fire_at") or "").strip()
        if next_fire_text:
            try:
                next_fire = datetime.fromisoformat(next_fire_text)
            except ValueError:
                next_fire = None

        last_fire_date = None
        last_fire_text = str(task.get("_runtime_last_fire_date") or "").strip()
        if last_fire_text:
            try:
                last_fire_date = datetime.fromisoformat(f"{last_fire_text}T00:00:00").date()
            except ValueError:
                last_fire_date = None

        week_cache = task.get("_runtime_week_cache")
        if not isinstance(week_cache, dict):
            week_cache = None
        month_cache = task.get("_runtime_month_cache")
        if not isinstance(month_cache, dict):
            month_cache = None
        return {
            "next_fire": next_fire,
            "last_fire_date": last_fire_date,
            "week_cache": week_cache,
            "month_cache": month_cache,
        }

    def _sync_random_runtime_state(self, task, state):
        if not isinstance(task, dict) or not isinstance(state, dict):
            return False
        changed = False
        next_fire = state.get("next_fire")
        next_fire_at = next_fire.replace(microsecond=0).isoformat() if isinstance(next_fire, datetime) else ""
        if task.get("next_fire_at") != next_fire_at:
            task["next_fire_at"] = next_fire_at
            changed = True
        if task.get("status") != "pending":
            task["status"] = "pending"
            changed = True
        last_fire_date = state.get("last_fire_date")
        last_fire_text = last_fire_date.isoformat() if last_fire_date else ""
        if task.get("_runtime_last_fire_date") != last_fire_text:
            task["_runtime_last_fire_date"] = last_fire_text
            changed = True
        week_cache = state.get("week_cache") or None
        if task.get("_runtime_week_cache") != week_cache:
            task["_runtime_week_cache"] = week_cache
            changed = True
        month_cache = state.get("month_cache") or None
        if task.get("_runtime_month_cache") != month_cache:
            task["_runtime_month_cache"] = month_cache
            changed = True
        return changed

    def _disable_once_material_outreach_task(self, task_id):
        task_id = str(task_id or "").strip()
        if not task_id:
            return
        for task in self.config.material_outreach_list or []:
            if not isinstance(task, dict):
                continue
            if str(task.get("id") or task.get("task_id") or "").strip() != task_id:
                continue
            task["enabled"] = False
            break
        self.config.config["material_outreach_list"] = self.config.material_outreach_list
        self._save_material_outreach_task_definitions_only(self.config.material_outreach_list)
        log(level="SUCCESS", message=f"一次性素材转发任务 {task_id} 已执行完毕，自动禁用")

    def _result_error_text(self, result, default="处理结果为空或失败，未返回详细错误"):
        if isinstance(result, dict):
            for key in ("message", "error", "detail", "reason"):
                text = str(result.get(key) or "").strip()
                if text:
                    return text
            return default
        if isinstance(result, BaseException):
            text = str(result).strip()
            return text or default
        if result is False or result is None:
            return default
        text = str(result).strip()
        return text or default

    def _material_outreach_preface_is_queued(self, result):
        return isinstance(result, dict) and str(result.get("status") or "").strip() == "queued_preface"

    def _material_outreach_is_stopped(self, result):
        return isinstance(result, dict) and str(result.get("status") or "").strip() == "stopped"

    def _material_outreach_is_deferred(self, result):
        if not isinstance(result, dict):
            return False
        status = str(result.get("status") or "").strip().lower()
        return status in {"deferred", "deferred_lock_busy"}

    def _material_outreach_is_uncertain(self, result):
        return isinstance(result, dict) and str(result.get("status") or "").strip().lower() == "uncertain"

    def _material_outreach_result_failed(self, result):
        if self._material_outreach_preface_is_queued(result):
            return False
        if self._material_outreach_is_stopped(result):
            return False
        if self._material_outreach_is_deferred(result):
            return False
        if isinstance(result, dict):
            status = str(result.get("status") or "").strip().lower()
            if status in {"failed", "error", "cancelled", "uncertain"}:
                return True
            if "success" in result:
                return not bool(result.get("success"))
            if "ok" in result:
                return not bool(result.get("ok"))
            return False
        return not bool(result)

    def _resolve_material_outreach_direct_failure(self, task_id, result, *, now=None):
        now = now or datetime.now()
        task_id = str(task_id or "").strip()
        if not task_id:
            return False
        tasks = list(getattr(self.config, "material_outreach_list", []) or [])
        changed = False
        error_text = self._result_error_text(result, default="素材转发执行失败")
        for task in tasks:
            if not isinstance(task, dict):
                continue
            current_task_id = str(task.get("id") or task.get("task_id") or "").strip()
            if current_task_id != task_id:
                continue
            normalized = normalize_material_outreach_task(task)
            trigger_strategy = normalize_trigger_strategy(
                normalized.get("trigger_strategy") or normalized.get("mode") or "fixed"
            )
            repeat_mode = str(normalized.get("repeat_mode") or "repeat").strip() or "repeat"
            if trigger_strategy == "random":
                state = self._load_random_runtime_state(task)
                state["last_fire_date"] = now.date()
                state["next_fire"] = None
                changed = self._sync_random_runtime_state(task, state) or changed
            else:
                plan = self._compile_fixed_runtime_plan(task, now=now)
                plan = advance_task_plan_after_success(plan, now=now)
                changed = self._sync_runtime_plan_fields(task, plan) or changed
            target_status = "" if repeat_mode == "once" else "pending"
            if task.get("status") != target_status:
                task["status"] = target_status
                changed = True
            if task.get("last_error") != error_text:
                task["last_error"] = error_text
                changed = True
            if repeat_mode == "once" and task.get("enabled", True):
                task["enabled"] = False
                if task.get("next_fire_at") != "":
                    task["next_fire_at"] = ""
                changed = True
                log(message=f"一次性素材转发任务 {task_id} 执行失败，已结束本轮任务")
            break
        if changed:
            next_tasks = [item for item in tasks if isinstance(item, dict)]
            self._set_runtime_task_list("material_outreach_list", next_tasks)
            self._save_material_outreach_task_definitions_only(next_tasks)
        return changed

    def _material_outreach_preface_cycle_records(self, task_id, *, run_id="", scheduled_at="", records=None):
        task_id = str(task_id or "").strip()
        run_id = str(run_id or "").strip()
        scheduled_at = str(scheduled_at or "").strip()
        if not task_id:
            return []
        matched = []
        source_records = self._load_material_outreach_preface_queue() if records is None else records
        for record in source_records:
            if str(record.get("task_id") or "").strip() != task_id:
                continue
            if run_id and str(record.get("run_id") or "").strip() != run_id:
                continue
            if scheduled_at and str(record.get("scheduled_at") or "").strip() != scheduled_at:
                continue
            matched.append(record)
        return matched

    def _has_material_outreach_preface_record(self, task_id, scheduled_at=""):
        return bool(
            self._material_outreach_preface_cycle_records(
                task_id,
                scheduled_at=scheduled_at,
            )
        )

    def _resolve_material_outreach_preface_cycle(
        self,
        task_id,
        *,
        run_id="",
        scheduled_at="",
        success_hint=None,
        now=None,
        cycle_records=None,
    ):
        now = now or datetime.now()
        task_id = str(task_id or "").strip()
        if not task_id:
            return False
        cycle_records = self._material_outreach_preface_cycle_records(
            task_id,
            run_id=run_id,
            scheduled_at=scheduled_at,
            records=cycle_records,
        )
        if not cycle_records:
            return False
        if any(str(item.get("status") or "").strip() == "pending" for item in cycle_records):
            return False
        cycle_success = all(
            str(item.get("status") or "").strip() == "sent" for item in cycle_records
        )
        tasks = list(getattr(self.config, "material_outreach_list", []) or [])
        changed = False
        for task in tasks:
            if not isinstance(task, dict):
                continue
            current_task_id = str(task.get("id") or task.get("task_id") or "").strip()
            if current_task_id != task_id:
                continue
            normalized = normalize_material_outreach_task(task)
            trigger_strategy = normalize_trigger_strategy(
                normalized.get("trigger_strategy") or normalized.get("mode") or "fixed"
            )
            repeat_mode = str(normalized.get("repeat_mode") or "repeat").strip() or "repeat"
            if cycle_success:
                if trigger_strategy == "random":
                    state = self._load_random_runtime_state(task)
                    state["last_fire_date"] = now.date()
                    state["next_fire"] = None
                    changed = self._sync_random_runtime_state(task, state) or changed
                else:
                    plan = self._compile_fixed_runtime_plan(task, now=now)
                    plan = advance_task_plan_after_success(plan, now=now)
                    changed = self._sync_runtime_plan_fields(task, plan) or changed
                if task.get("last_error") != "":
                    task["last_error"] = ""
                    changed = True
                if repeat_mode == "once" and task.get("enabled", True):
                    task["enabled"] = False
                    if task.get("next_fire_at") != "":
                        task["next_fire_at"] = ""
                    if task.get("status") != "pending":
                        task["status"] = "pending"
                    changed = True
                    log(level="SUCCESS", message=f"一次性素材转发任务 {task_id} 已执行完毕，自动禁用")
            else:
                error_text = ""
                for item in cycle_records or [record]:
                    error_text = str(item.get("error") or item.get("preface_error") or "").strip()
                    if error_text:
                        break
                if not error_text and success_hint is False:
                    error_text = "AI 前置文案队列未完成"
                if trigger_strategy == "random":
                    state = self._load_random_runtime_state(task)
                    state["last_fire_date"] = now.date()
                    state["next_fire"] = None
                    changed = self._sync_random_runtime_state(task, state) or changed
                else:
                    plan = self._compile_fixed_runtime_plan(task, now=now)
                    plan = advance_task_plan_after_success(plan, now=now)
                    changed = self._sync_runtime_plan_fields(task, plan) or changed
                if task.get("status") != ("" if repeat_mode == "once" else "pending"):
                    task["status"] = "" if repeat_mode == "once" else "pending"
                    changed = True
                if error_text and task.get("last_error") != error_text:
                    task["last_error"] = error_text
                    changed = True
                if repeat_mode == "once" and task.get("enabled", True):
                    task["enabled"] = False
                    if task.get("next_fire_at") != "":
                        task["next_fire_at"] = ""
                    changed = True
            break
        if changed:
            next_tasks = [item for item in tasks if isinstance(item, dict)]
            self._set_runtime_task_list("material_outreach_list", next_tasks)
            self._save_material_outreach_task_definitions_only(next_tasks)
        return changed

    def _sync_material_outreach_task_after_preface_result(self, record, *, success, now=None):
        record = record if isinstance(record, dict) else {}
        return self._resolve_material_outreach_preface_cycle(
            record.get("task_id"),
            run_id=record.get("run_id"),
            scheduled_at=record.get("scheduled_at"),
            success_hint=success,
            now=now,
        )

    def _material_outreach_task_preface_mode(self, task):
        return normalize_material_outreach_preface_config(task).get("preface_mode", "none")

    def _material_outreach_queue_time_due(self, task, scheduled_at, *, now=None):
        if self._material_outreach_task_preface_mode(task) != "ai":
            return False
        try:
            scheduled_dt = datetime.fromisoformat(str(scheduled_at or "").strip())
        except ValueError:
            return False
        now = now or datetime.now()
        return now >= scheduled_dt - timedelta(seconds=30)

    def _scheduled_message_selector_from_task(self, task):
        mode = str((task or {}).get("targets_mode") or "manual").strip() or "manual"
        include_tags = [
            str(tag or "").strip()
            for tag in ((task or {}).get("target_tags") or [])
            if str(tag or "").strip()
        ]
        exclude_tags = [
            str(tag or "").strip()
            for tag in ((task or {}).get("exclude_target_tags") or [])
            if str(tag or "").strip()
        ]
        if mode == "include":
            return {"base": "all_friends" if include_tags else "manual", "include_tags": include_tags, "exclude_tags": []}
        if mode == "exclude":
            return {"base": "all_friends", "include_tags": [], "exclude_tags": exclude_tags}
        if mode == "all":
            return {"base": "all_friends", "include_tags": [], "exclude_tags": []}
        return {"base": "manual", "include_tags": [], "exclude_tags": []}

    def _resolve_scheduled_message_task_targets(self, task):
        task = task if isinstance(task, dict) else {}
        mode = str(task.get("targets_mode") or "manual").strip() or "manual"
        manual_names = [
            str(name or "").strip()
            for name in (task.get("manual_target_names") or [])
            if str(name or "").strip()
        ]
        manual_keys = {
            str(key or "").strip()
            for key in (task.get("targets") or [])
            if str(key or "").strip()
        }
        def unique_send_names(values):
            names = []
            emitted = set()
            for value in values or []:
                send_name = str(value or "").strip()
                if send_name and send_name not in emitted:
                    names.append(send_name)
                    emitted.add(send_name)
            return names

        def target_send_name(contact):
            if isinstance(contact, dict):
                return contact_send_target(contact)
            return str(contact or "").strip()

        directory, _directory_file, _wx_id = self._load_contact_profiles_directory()
        if mode not in {"all", "include", "exclude"}:
            selected = [
                contact for contact in (directory.get("subjects") or [])
                if isinstance(contact, dict) and str(contact.get("contact_key") or "").strip() in manual_keys
            ]
            manual = resolve_manual_target_names(directory, manual_names)
            selected.extend(manual.get("selected") or [])
            return unique_send_names(
                [target_send_name(contact) for contact in selected if target_send_name(contact)]
                + [str(name or "").strip() for name in (manual.get("missing") or [])]
            )
        resolved = resolve_target_selector(directory, self._scheduled_message_selector_from_task(task))
        selected = list(resolved.get("selected") or [])
        selected.extend([
            contact for contact in (directory.get("subjects") or [])
            if isinstance(contact, dict) and str(contact.get("contact_key") or "").strip() in manual_keys
        ])
        if mode != "all" and manual_names:
            manual = resolve_manual_target_names(directory, manual_names)
            selected_by_key = set()
            selected_by_send_name = set()
            for contact in selected:
                if not isinstance(contact, dict):
                    continue
                key = str(contact.get("contact_key") or "").strip()
                send_name = target_send_name(contact)
                if key:
                    selected_by_key.add(key)
                if send_name:
                    selected_by_send_name.add(send_name)
            if mode == "exclude":
                excluded_keys = set()
                excluded_names = set()
                for contact in manual.get("selected") or []:
                    if not isinstance(contact, dict):
                        continue
                    key = str(contact.get("contact_key") or "").strip()
                    send_name = target_send_name(contact)
                    if key:
                        excluded_keys.add(key)
                    if send_name:
                        excluded_names.add(send_name)
                for name in manual.get("missing") or []:
                    send_name = str(name or "").strip()
                    if send_name:
                        excluded_names.add(send_name)
                selected = [
                    contact for contact in selected
                    if not (
                        str(contact.get("contact_key") or "").strip() in excluded_keys
                        or target_send_name(contact) in excluded_names
                    )
                ]
            else:
                for contact in manual.get("selected") or []:
                    if not isinstance(contact, dict):
                        continue
                    key = str(contact.get("contact_key") or "").strip()
                    send_name = target_send_name(contact)
                    if (key and key in selected_by_key) or (send_name and send_name in selected_by_send_name):
                        continue
                    selected.append(contact)
                    if key:
                        selected_by_key.add(key)
                    if send_name:
                        selected_by_send_name.add(send_name)
                for name in manual.get("missing") or []:
                    send_name = str(name or "").strip()
                    if send_name and send_name not in selected_by_send_name:
                        selected.append({"send_target": send_name, "name": send_name})
                        selected_by_send_name.add(send_name)
        return unique_send_names([
            target_send_name(contact)
            for contact in selected
            if target_send_name(contact)
        ])

    def _resolve_scheduled_message_task_target_records(self, task):
        names = self._resolve_scheduled_message_task_targets(task)
        mode = str((task or {}).get("targets_mode") or "manual").strip() or "manual"
        require_all_contact_keys = mode in {"all", "include", "exclude"}
        selected_keys = {
            str(key or "").strip()
            for key in ((task or {}).get("targets") or [])
            if str(key or "").strip()
        }
        directory, _directory_file, _wx_id = self._load_contact_profiles_directory()
        matches_by_name = {}
        for contact in directory.get("subjects") or []:
            if not isinstance(contact, dict):
                continue
            send_name = contact_send_target(contact)
            if send_name:
                matches_by_name.setdefault(send_name, []).append(contact)
        keyed_names = {
            contact_send_target(contact)
            for contact in (directory.get("subjects") or [])
            if isinstance(contact, dict)
            and str(contact.get("contact_key") or "").strip() in selected_keys
            and contact_send_target(contact)
        }
        records = []
        for name in names:
            matches = matches_by_name.get(name) or []
            contact_key = ""
            if len(matches) == 1:
                contact_key = str(matches[0].get("contact_key") or "").strip()
            records.append({
                "contact_key": contact_key,
                "send_name": name,
                "display_name": contact_display_label(matches[0]) if len(matches) == 1 else name,
                "require_contact_key": bool(contact_key) and (require_all_contact_keys or name in keyed_names),
            })
        return records

    def _run_due_scheduled_message_tasks(self, now=None):
        runtime_task_runner.run_due_scheduled_message_tasks(self, now=now)

    def _run_due_fixed_material_outreach(self, now=None):
        runtime_task_runner.run_due_fixed_material_outreach(self, now=now)

    def _run_due_random_material_outreach(self, now=None):
        runtime_task_runner.run_due_random_material_outreach(self, now=now)




    def _process_unified_runtime_tasks(self, now=None):
        runtime_task_runner.process_unified_runtime_tasks(self, now=now)

    def _process_pending_runtime_task_reload(self):
        return runtime_task_runner.process_pending_runtime_task_reload(self)

    def _listen_add_error(self, result):
        return listening.listen_add_error(result)

    def _subwindow_who(self, chat):
        return listening.subwindow_who(chat)

    def _get_verified_subwindow(self, nickname, *, chat_type=None):
        return listening.get_verified_subwindow(
            self,
            nickname,
            chat_type=chat_type,
        )

    def _try_get_all_subwindow_names(self):
        return listening.try_get_all_subwindow_names(self)

    def _add_listen_chat_once(self, nickname, label, *, chat_type=None):
        return listening.add_listen_chat_once(
            self,
            nickname,
            label,
            chat_type=chat_type,
        )

    def _add_and_verify_subwindow(self, nickname, retry_count=3, *, chat_type=None):
        return listening.add_and_verify_subwindow(
            self,
            nickname,
            retry_count=retry_count,
            chat_type=chat_type,
        )

    def _expected_listener_names(self):
        return listening.expected_listener_names(self)

    def _ensure_listener_subwindow(self, nickname, retry_count=3, *, chat_type=None):
        return listening.ensure_listener_subwindow(
            self,
            nickname,
            retry_count=retry_count,
            chat_type=chat_type,
        )

    def _reconcile_listener_subwindows(self, retry_count=3):
        return listening.reconcile_listener_subwindows(self, retry_count=retry_count)

    def _maybe_reconcile_listener_subwindows(self, force=False, retry_count=3):
        return listening.maybe_reconcile_listener_subwindows(self, force=force, retry_count=retry_count)

    def _remove_listen_chat_verified(
        self,
        nickname,
        *,
        chat_type=None,
        log_success=True,
    ):
        return listening.remove_listen_chat_verified(
            self,
            nickname,
            chat_type=chat_type,
            log_success=log_success,
        )

    def _close_dynamic_listener_subwindows(self, nicknames):
        return listening.close_dynamic_listener_subwindows(self, nicknames)

    def _verify_initial_listeners(self, expected_chats, retry_count=3):
        return listening.verify_initial_listeners(self, expected_chats, retry_count=retry_count)

    def init_wx_listeners(self):
        return listening.init_wx_listeners(self)

    def _arm_listener_auto_recovery(self, exc, source=""):
        return listening.arm_listener_auto_recovery(self, exc, source=source)

    def _process_listener_auto_recovery(self):
        return listening.process_listener_auto_recovery(self)

    def send_material_outreach(self, task):
        if self.is_stop_requested():
            return {"status": "stopped", "message": "机器人正在停止，已跳过素材转发"}
        task = task or {}
        task_id = str(task.get("task_id") or task.get("id") or "").strip()
        unresolved = self._material_outreach_store().freeze_interrupted_sends(task_id)
        if unresolved:
            return {
                "status": "uncertain",
                "message": "上次素材转发结果待人工确认，已禁止自动重发",
                "uncertain_count": len(unresolved),
            }
        if not should_send_scheduled_message(
            task.get("repeat_type", "daily"),
            task.get("weekdays", []),
            task.get("dates", []),
            datetime.now(),
        ):
            return False
        context = getattr(self, "_material_ui_context", None)
        if context is None:
            context = threading.local()
            self._material_ui_context = context
        task_key = str(task.get("_ui_task_key") or "")
        task_version = int(task.get("_ui_task_version") or 0)
        previous = getattr(context, "value", None)
        context.value = {"task_key": task_key, "task_version": task_version}
        try:
            return self._send_material_outreach_locked(task)
        finally:
            context.value = previous

    def _material_ui_task_is_stale(self):
        context = getattr(getattr(self, "_material_ui_context", None), "value", None) or {}
        task_key = str(context.get("task_key") or "")
        task_version = int(context.get("task_version") or 0)
        return bool(task_key and task_version and self._current_ui_task_version(task_key) != task_version)

    def _material_outreach_task_label(self, task):
        task = task or {}
        return str(
            task.get("task_name")
            or task.get("name")
            or task.get("task_id")
            or task.get("id")
            or "未命名任务"
        ).strip() or "未命名任务"

    def _material_outreach_strategy_label(self, task):
        strategy = str((task or {}).get("batch_material_strategy") or "per_batch").strip()
        return {
            "per_batch": "按批次随机",
            "per_run": "按任务随机",
            "per_task": "按任务随机",
            "fixed": "手动选择",
        }.get(strategy, strategy or "按批次随机")

    def _material_outreach_progress_summary(self, snapshot):
        snapshot = snapshot or {}
        run_id = str(snapshot.get("run_id") or "").strip()
        targets = list(snapshot.get("targets") or [])
        summary = {
            "targets": len(targets),
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "pending": 0,
        }
        if not run_id:
            return summary

        latest_by_key = {}
        for record in self._load_material_progress_records():
            if str(record.get("run_id") or "").strip() != run_id:
                continue
            key = str(record.get("contact_key") or record.get("send_name") or "").strip()
            if key:
                latest_by_key[key] = record

        for record in latest_by_key.values():
            status = str(record.get("status") or "").strip()
            if status == "success":
                summary["success"] += 1
            elif status == "failed":
                summary["failed"] += 1
            elif status == "skipped":
                summary["skipped"] += 1
            elif status == "pending":
                summary["pending"] += 1
        return summary

    def _log_material_outreach_run_start(self, task, snapshot):
        snapshot = snapshot or {}
        target_count = len(list(snapshot.get("targets") or []))
        log(
            message=(
                f"[素材转发] 开始任务 {self._material_outreach_task_label(task)}："
                f"目标 {target_count} 人，素材策略：{self._material_outreach_strategy_label(task)}"
            )
        )

    def _log_material_outreach_run_finish(self, task, snapshot):
        summary = self._material_outreach_progress_summary(snapshot)
        complete_success = bool(
            summary["success"] > 0
            and summary["failed"] == 0
            and summary["pending"] == 0
        )
        log(
            level="SUCCESS" if complete_success else "WARNING",
            message=(
                f"[素材转发] 任务 {self._material_outreach_task_label(task)} 完成："
                f"成功 {summary['success']}，失败 {summary['failed']}，跳过 {summary['skipped']}"
            )
        )

    def _send_material_outreach_locked(self, task):
        send_records = self._load_material_send_records()
        original_task = dict(task or {})
        task = self._resolve_material_outreach_directory_task(task, send_records)
        context = getattr(getattr(self, "_material_ui_context", None), "value", None)
        if isinstance(context, dict) and isinstance(task, dict):
            snapshot = task.get("_outreach_target_snapshot") or {}
            by_name = {}
            duplicate_names = set()
            for contact in snapshot.get("pending_targets") or snapshot.get("targets") or []:
                if not isinstance(contact, dict):
                    continue
                send_name = str(contact.get("send_name") or "").strip()
                if not send_name:
                    continue
                if send_name in by_name:
                    duplicate_names.add(send_name)
                by_name[send_name] = contact
            for name in duplicate_names:
                by_name.pop(name, None)
            context["contacts_by_name"] = by_name
        snapshot = (
            task.get("_outreach_target_snapshot")
            if isinstance(task, dict)
            else getattr(self, "_last_material_outreach_target_snapshot", None)
        )
        if snapshot:
            self._log_material_outreach_run_start(task or original_task, snapshot)
        if not task:
            if snapshot:
                self._log_material_outreach_run_finish(original_task, snapshot)
            return True
        if task.get("batch_material_strategy") == "fixed" and not str(task.get("fixed_material_id") or "").strip():
            result = self._attempt_material_outreach_batches(task, send_records, allow_rebuild=False)
            if snapshot and not self._material_outreach_is_deferred(result):
                self._log_material_outreach_run_finish(task, snapshot)
            return result
        send_records = self._load_material_send_records()
        result = self._attempt_material_outreach_batches(task, send_records, allow_rebuild=True)
        if (
            self._material_outreach_is_deferred(result)
            or self._material_outreach_is_stopped(result)
            or self._material_outreach_is_uncertain(result)
            or self._material_outreach_preface_is_queued(result)
        ):
            return result
        success = bool(result)
        if snapshot:
            self._log_material_outreach_run_finish(task, snapshot)
        return success

    def _contact_profiles_directory_file(self):
        return contacts.contact_profiles_directory_file(self)

    def _load_contact_profiles_directory(self):
        return contacts.load_contact_profiles_directory(self)

    def _contact_profiles_remark_repair_records_file(self):
        return contacts.contact_profiles_remark_repair_records_file(self)

    def _contact_directory_auto_maintenance_enabled(self):
        return contacts.contact_directory_auto_maintenance_enabled(self)

    def _contact_directory_auto_maintenance_batch_size_value(self):
        return contacts.contact_directory_auto_maintenance_batch_size_value(self)

    def _contact_directory_auto_maintenance_interval_minutes_value(self):
        return contacts.contact_directory_auto_maintenance_interval_minutes_value(self)

    def _contact_directory_auto_maintenance_full_scan_interval_days_value(self):
        return contacts.contact_directory_auto_maintenance_full_scan_interval_days_value(self)

    def _contact_directory_auto_maintenance_window_start_value(self):
        return contacts.contact_directory_auto_maintenance_window_start_value(self)

    def _contact_directory_auto_maintenance_window_end_value(self):
        return contacts.contact_directory_auto_maintenance_window_end_value(self)

    def _maintenance_now(self, now=None):
        return contacts.maintenance_now(now)

    def _contact_directory_auto_maintenance_time_window_allows(self, now=None):
        return contacts.contact_directory_auto_maintenance_time_window_allows(self, now=now)

    def _is_contact_directory_auto_maintenance_idle(self):
        return contacts.is_contact_directory_auto_maintenance_idle(self)

    def _contact_directory_auto_cycle_state(self, directory):
        return contacts.contact_directory_auto_cycle_state(directory)

    def _write_contact_directory_auto_cycle_state(self, directory, *, now=None, **updates):
        return contacts.write_contact_directory_auto_cycle_state(directory, now=now, **updates)

    def _save_contact_profiles_directory(self, directory):
        return contacts.save_contact_profiles_directory(self, directory)

    def _refresh_contact_profiles_single_batch(
        self,
        mode="standard",
        start_name="",
        start_identity="",
        interval=None,
        *,
        use_saved_position=False,
        count_override=None,
        log_start_finish=True,
        previous_next_start_name="",
        run_kind="manual_standard",
        logical_start_name=None,
        switch_back_to_chat=True,
    ):
        return contacts.refresh_contact_profiles_single_batch(
            self,
            mode=mode,
            start_name=start_name,
            start_identity=start_identity,
            interval=interval,
            use_saved_position=use_saved_position,
            count_override=count_override,
            log_start_finish=log_start_finish,
            previous_next_start_name=previous_next_start_name,
            run_kind=run_kind,
            logical_start_name=logical_start_name,
            switch_back_to_chat=switch_back_to_chat,
        )

    def refresh_contact_profiles_batch(
        self,
        mode="standard",
        start_name="",
        start_identity="",
        interval=None,
        *,
        use_saved_position=False,
        count_override=None,
        run_to_completion=False,
        automatic=False,
    ):
        return contacts.refresh_contact_profiles_batch(
            self,
            mode=mode,
            start_name=start_name,
            start_identity=start_identity,
            interval=interval,
            use_saved_position=use_saved_position,
            count_override=count_override,
            run_to_completion=run_to_completion,
            automatic=automatic,
        )

    def _check_contact_directory_auto_maintenance(self, now=None):
        return contacts.check_contact_directory_auto_maintenance(self, now=now)

    def relationship_scan_payload(self):
        state = relationship_scan.load_state(self.config.DATA_DIR, str(getattr(self, "wx_id", "") or "default"))
        return relationship_scan.relationship_scan_payload(state)

    def _sync_relationship_state_from_contact_directory(self, _directory=None):
        wx_id = str(getattr(self, "wx_id", "") or "default")
        try:
            state = relationship_scan.load_state(self.config.DATA_DIR, wx_id)
            if not state.get("records"):
                return None
            updated_state = relationship_scan.apply_state_to_local_contacts(self, state)
            if updated_state != state:
                relationship_scan.save_state(self.config.DATA_DIR, updated_state)
            directory, _directory_file, _wx_id = self._load_contact_profiles_directory()
            return directory
        except Exception as exc:
            log(level="WARNING", message=f"关系扫描状态回填通讯录失败：{exc}")
            return None

    def scan_relationship_sessions(self):
        result = relationship_scan.scan_current_sessions(self, mode="manual")
        self._sync_relationship_metrics()
        return result

    def full_scan_relationship_sessions(self, *, allow_running=False):
        result = relationship_scan.scan_full_sessions(self, allow_running=allow_running)
        self._sync_relationship_metrics()
        return result

    def _check_relationship_auto_scan(self, now=None):
        result = relationship_scan.check_auto_scan(self, now=now)
        self._sync_relationship_metrics(now=now)
        return result

    def _process_relationship_tag_sync(self):
        return relationship_scan.process_pending_wechat_tag_sync(self)

    def friend_request_payload(self):
        state = friend_request.load_state(self.config.DATA_DIR, str(getattr(self, "wx_id", "") or "default"))
        return friend_request.friend_request_payload(state)

    def run_friend_request_once(self, force=False):
        return friend_request.run_once(self, force=force)

    def _check_friend_request_auto_run(self, now=None):
        return friend_request.check_auto_run(self, now=now)

    def _sync_relationship_metrics(self, now=None):
        try:
            state = relationship_scan.load_state(
                self.config.DATA_DIR,
                str(getattr(self, "wx_id", "") or "default"),
            )
            summary = relationship_scan.relationship_scan_summary(state, now=now)
            self._metric_set_today_relationship_counts(
                blocked=summary.get("today_blocked", 0),
                deleted=summary.get("today_deleted", 0),
                now=now,
            )
        except Exception:
            pass

    def set_contact_profiles_paused(self, paused=True):
        return contacts.set_contact_profiles_paused(self, paused=paused)

    def _resolve_material_outreach_directory_task(self, task, send_records):
        selector = (task or {}).get("target_selector")
        if not isinstance(selector, dict):
            return task

        directory_file, wx_id = self._contact_profiles_directory_file()
        directory = load_contact_directory(directory_file, wx_id=wx_id)
        resolved = resolve_target_selector(directory, selector)
        manual_names = (task or {}).get("manual_target_names") or []
        mode = str(selector.get("mode") or "").strip()
        if mode not in {"all", "include", "exclude"}:
            if selector.get("include_tags") or selector.get("base") == "manual":
                mode = "include"
            elif selector.get("exclude_tags"):
                mode = "exclude"
            else:
                mode = "all"
        def target_send_name(contact):
            if isinstance(contact, dict):
                return contact_send_target(contact)
            return str(contact or "").strip()
        if manual_names:
            manual = resolve_manual_target_names(directory, manual_names)
            selected = list(resolved.get("selected") or [])
            if mode == "exclude":
                excluded_keys = set()
                excluded_names = set()
                for contact in manual.get("selected") or []:
                    if not isinstance(contact, dict):
                        continue
                    key = str(contact.get("contact_key") or "")
                    send_name = target_send_name(contact)
                    if key:
                        excluded_keys.add(key)
                    if send_name:
                        excluded_names.add(send_name)
                for name in manual.get("missing") or []:
                    send_name = str(name or "").strip()
                    if send_name:
                        excluded_names.add(send_name)
                selected = [
                    contact for contact in selected
                    if not (
                        str(contact.get("contact_key") or "") in excluded_keys
                        or target_send_name(contact) in excluded_names
                    )
                ]
            else:
                seen_keys = {
                    str(item.get("contact_key") or "")
                    for item in selected
                    if isinstance(item, dict) and str(item.get("contact_key") or "")
                }
                seen_names = {
                    target_send_name(item)
                    for item in selected
                    if isinstance(item, dict) and target_send_name(item)
                }
                for contact in manual.get("selected") or []:
                    if not isinstance(contact, dict):
                        continue
                    key = str(contact.get("contact_key") or "")
                    send_name = target_send_name(contact)
                    if (key and key in seen_keys) or (send_name and send_name in seen_names):
                        continue
                    selected.append(contact)
                    if key:
                        seen_keys.add(key)
                    if send_name:
                        seen_names.add(send_name)
                for name in manual.get("missing") or []:
                    send_name = str(name or "").strip()
                    if not send_name or send_name in seen_names:
                        continue
                    selected.append(
                        {
                            "contact_key": "",
                            "send_target": send_name,
                            "name": send_name,
                            "tags": [],
                            "warnings": [],
                        }
                    )
                    seen_names.add(send_name)
            resolved["selected"] = selected
            resolved["warnings"] = list(resolved.get("warnings") or []) + list(manual.get("warnings") or [])
        snapshot_task = dict(task or {})
        snapshot_task["send_records"] = list(send_records or [])
        snapshot = build_target_snapshot(snapshot_task, resolved, now=datetime.now())
        progress_records = snapshot.get("progress_records") or []
        self._last_material_outreach_target_snapshot = snapshot
        self._last_material_outreach_progress_records = progress_records
        self._append_material_progress_records(progress_records, limit=1000)

        missing = [
            item for item in resolved.get("excluded") or []
            if isinstance(item, dict) and item.get("reason") == "missing_contact"
        ]
        if missing:
            for item in missing:
                contact = item.get("contact") if isinstance(item.get("contact"), dict) else {}
                target = contact_display_label(contact) or contact.get("contact_key") or ""
                skip = build_skip_record(
                    snapshot_task.get("task_id"),
                    target,
                    "missing_contact",
                    "未找到通讯录联系人，已阻断本轮主动转发",
                )
                self._append_material_skip_record(skip, limit=1000)
                log(message=f"[素材转发] 跳过 {target}：{skip.get('detail')}")
            return None

        send_names = send_names_from_target_snapshot(snapshot)
        resolved_task = dict(task or {})
        resolved_task["targets"] = send_names
        resolved_task["_outreach_target_snapshot"] = snapshot
        return resolved_task

    def repair_contact_profile_remarks(self, contact_keys=None):
        return contacts.repair_contact_profile_remarks(self, contact_keys=contact_keys)

    def _contact_repair_before_display(self, contact):
        return contacts.contact_repair_before_display(contact)

    def preview_contact_profile_remark_repairs(self, contact_keys=None):
        return contacts.preview_contact_profile_remark_repairs(self, contact_keys=contact_keys)

    def _attempt_material_outreach_batches(self, task, send_records, *, allow_rebuild):
        if self.is_stop_requested():
            return {"status": "stopped", "message": "机器人正在停止，已跳过素材转发"}
        if self._material_ui_task_is_stale():
            return {"status": "cancelled", "message": "素材转发任务已更新或取消"}
        sources = material_sources_for_task(task, getattr(self.config, "material_source_list", []) or [])
        if not sources:
            for target in task.get("targets", []) or []:
                skip = build_skip_record(task.get("task_id"), target, "no_material", "没有配置素材来源")
                self._append_material_skip_record(skip, limit=1000)
                self._append_material_outreach_skip_progress(task, skip)
                log(message=f"[素材转发] 跳过 {target}：{skip.get('detail')}")
            return False
        original_send_records = list(send_records or [])
        materials = self._load_material_outreach_materials()
        if allow_rebuild:
            for source in sources:
                materials = self._rebuild_material_runtime_pool_for_source(source, goback=True)

        plan = plan_material_outreach_batches(
            task,
            materials,
            original_send_records,
            self._material_runtime_messages.keys(),
            now=datetime.now(),
        )
        for skip in plan["skip"]:
            if not allow_rebuild and skip.get("reason") == "no_material":
                continue
            self._append_material_skip_record(skip, limit=1000)
            self._append_material_outreach_skip_progress(task, skip)
            log(message=f"[素材转发] 跳过 {skip.get('target')}：{skip.get('detail')}")
        if any(skip.get("reason") == "fixed_material_missing" for skip in plan["skip"]):
            return True
        if not plan["send"]:
            return False
        all_success = True
        has_preface_queue = False
        for action in plan["send"]:
            if self.is_stop_requested():
                return {"status": "stopped", "message": "机器人正在停止，已停止后续素材转发"}
            if self._material_ui_task_is_stale():
                return {"status": "cancelled", "message": "素材转发任务已更新或取消"}
            material = action.get("material") if isinstance(action, dict) else {}
            material_source = str((material or {}).get("source") or "").strip()
            if material_source:
                with self._get_material_source_read_lock(material_source):
                    result = self._send_material_outreach_action(task, action, materials)
            else:
                result = self._send_material_outreach_action(task, action, materials)
            if self._material_ui_task_is_stale():
                return {"status": "cancelled", "message": "素材转发任务已更新或取消"}
            if self._material_outreach_is_deferred(result):
                return result
            if self._material_outreach_is_uncertain(result):
                return result
            if self._material_outreach_preface_is_queued(result):
                has_preface_queue = True
                continue
            success = bool(result)
            all_success = all_success and success
        if all_success and has_preface_queue:
            return {"status": "queued_preface"}
        if all_success:
            return True
        if not allow_rebuild:
            log(message="[素材转发] 运行时素材发送失败，准备重建素材池重试")
            return False
        return all_success

    def _append_material_outreach_skip_progress(self, task, skip):
        snapshot = task.get("_outreach_target_snapshot") if isinstance(task, dict) else None
        if not snapshot:
            return
        target = str((skip or {}).get("target") or "").strip()
        if not target:
            return
        contact = None
        for item in snapshot.get("targets") or []:
            if str(item.get("send_name") or "").strip() == target:
                contact = item
                break
        if not contact:
            contact = {"contact_key": "", "send_name": target, "display_name": target, "warnings": []}
        record = build_progress_record(
            snapshot.get("run_id"),
            snapshot.get("task_id"),
            contact,
            "skipped",
            reason=(skip or {}).get("reason", ""),
            detail=(skip or {}).get("detail", ""),
            now=datetime.now(),
        )
        self._append_material_progress_records([record], limit=1000)

    def _rebuild_material_runtime_pool_for_source(self, source, *, goback=True):
        source = str(source or "").strip()
        if not source:
            return self._load_material_outreach_materials()
        limit = self._material_pool_limit_for_source(source)
        read_limit = limit + 10
        existing_materials = self._load_material_outreach_materials()
        messages = self._read_material_source_messages(source, read_limit, goback=goback)
        if not any(is_forwardable_material_message(message) for message in messages or []):
            if any(str((item or {}).get("source") or "").strip() == source for item in existing_materials or []):
                read_strategy = self._material_source_read_strategy(source)
                log(
                    level="WARNING",
                    message=(
                        f"[素材转发] 重建素材池未读取到可转发素材，已保留旧素材池："
                        f"来源 {source}，读取 {read_limit} 条，读取方案：{read_strategy}"
                    ),
                )
                return existing_materials
        materials, runtime_messages, rebuilt = rebuild_material_pool_for_source(
            existing_materials,
            source,
            messages,
            limit=limit,
            limit_map=getattr(self.config, "material_source_pool_limit_map", {}) or {},
            material_id_factory=lambda: f"mat_{uuid.uuid4().hex}",
        )
        material_source_by_id = {item.get("id"): item.get("source") for item in materials or []}
        kept_runtime_messages = {
            material_id: message
            for material_id, message in (getattr(self, "_material_runtime_messages", {}) or {}).items()
            if material_source_by_id.get(material_id) and material_source_by_id.get(material_id) != source
        }
        kept_runtime_messages.update(runtime_messages)
        self._material_runtime_messages = kept_runtime_messages
        self._save_material_outreach_materials(materials)
        read_strategy = self._material_source_read_strategy(source)
        log(
            message=(
                f"[素材转发] 已重建素材池：来源 {source}，读取 {read_limit} 条，"
                f"可用 {len(rebuilt)} 条，上限 {limit}，读取方案：{read_strategy}"
            )
        )
        return materials

    def _rebuild_material_pool_for_source(self, source, *, goback=True):
        return self._rebuild_material_runtime_pool_for_source(source, goback=goback)

    def _material_source_chat_type(self, source):
        source = str(source or "").strip()
        return (
            "group"
            if getattr(self.config, "group_switch", False)
            and source in (getattr(self.config, "group", []) or [])
            else "private"
        )

    def _read_material_source_messages(
        self,
        source,
        limit,
        *,
        chat_type=None,
        goback=True,
        target_signature="",
        require_forwardable=True,
    ):
        source = str(source or "").strip()
        chat_type = str(chat_type or self._material_source_chat_type(source))
        limit = max(1, int(limit or 1))
        result = self._ui_owner.call(
            wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.MATERIAL_READ,
                {
                    "conversation": source,
                    "chat_type": chat_type,
                    "limit": limit,
                    "goback": bool(goback),
                    "target_signature": str(target_signature or ""),
                    "require_forwardable": bool(require_forwardable),
                },
            ),
            wechat_ui_actions.UI_CALL_WAIT_TIMEOUT,
        )
        self._set_material_source_read_strategy(source, str((result or {}).get("strategy") or "未知"))
        return list((result or {}).get("messages") or [])

    def _set_material_source_read_strategy(self, source, strategy):
        source = str(source or "").strip()
        strategy = str(strategy or "").strip() or "未知"
        if not hasattr(self, "_material_source_read_strategies") or self._material_source_read_strategies is None:
            self._material_source_read_strategies = {}
        if source:
            self._material_source_read_strategies[source] = strategy

    def _material_source_read_strategy(self, source):
        source = str(source or "").strip()
        strategy = ""
        if hasattr(self, "_material_source_read_strategies") and isinstance(self._material_source_read_strategies, dict):
            strategy = str(self._material_source_read_strategies.get(source) or "").strip()
        return strategy or "未知"

    def _material_pool_limit_for_source(self, source):
        return material_pool_limit_for_source(
            getattr(self.config, "material_source_pool_limit_map", {}) or {},
            source,
        )

    def _restore_material_source_position(self, source=None, *, attempts=2, retry_delay=1.0):
        last_error = None
        for attempt in range(max(1, int(attempts or 1))):
            try:
                self._read_material_source_messages(
                    source,
                    1,
                    goback=True,
                    require_forwardable=False,
                )
                if attempt:
                    log(message=f"[素材转发] 恢复素材源最新位置成功：{source}")
                return True
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    log(level="WARNING", message=f"[素材转发] 恢复素材源最新位置失败，准备重试：{exc}")
                    time.sleep(max(0, float(retry_delay or 0)))
        log(level="WARNING", message=f"[素材转发] 恢复素材源最新位置最终失败：{last_error}")
        return False

    def _queue_material_outreach_preface_action(self, task, action):
        scheduled_at = str((task or {}).get("_preface_scheduled_at") or "").strip()
        if not scheduled_at:
            scheduled_at = datetime.now().replace(microsecond=0).isoformat()
        with self._material_outreach_runtime_lock():
            queue_records = self._load_material_outreach_preface_queue()
            record = build_preface_queue_record(
                task,
                action,
                scheduled_at=scheduled_at,
                now=datetime.now(),
                queue_id_factory=lambda: f"preface_{uuid.uuid4().hex[:8]}",
            )
            queue_records.append(record)
            self._save_material_outreach_preface_queue(queue_records)
        log(
            message=(
                f"[素材转发] {record.get('material_id') or '素材'} -> {record.get('target')} "
                f"已加入 AI 文案预生成队列，计划发送时间：{record.get('scheduled_at')}"
            )
        )
        return {"status": "queued_preface", "queue_id": record.get("queue_id", "")}

    def _find_material_outreach_material_for_queue(self, record, materials):
        material_id = str((record or {}).get("material_id") or "").strip()
        stable_signature = str((record or {}).get("stable_signature") or "").strip()
        for material in materials or []:
            if not isinstance(material, dict):
                continue
            if str(material.get("status", "active") or "").strip() != "active":
                continue
            if material_id and str(material.get("id") or "").strip() == material_id:
                return material
            if stable_signature and str(material.get("stable_signature") or "").strip() == stable_signature:
                return material
        return None

    def _material_forward_error_needs_refresh(self, error):
        text = str(error or "").strip()
        if not text:
            return False
        return any(
            marker in text
            for marker in (
                "消息对象已失效",
                "素材运行时句柄不存在",
                "句柄不存在",
                "对象已失效",
                "Element not found",
                "Find Control Timeout",
            )
        )

    def _refresh_material_runtime_message(self, material, materials=None):
        material = material if isinstance(material, dict) else {}
        source = str(material.get("source") or "").strip()
        stable_signature = str(material.get("stable_signature") or "").strip()
        material_id = str(material.get("id") or "").strip()
        if source and stable_signature:
            limit = self._material_pool_limit_for_source(source) + 10
            messages = self._read_material_source_messages(
                source,
                limit,
                goback=True,
                target_signature=stable_signature,
            )
            matched_message = None
            for candidate in messages or []:
                if build_stable_material_signature(candidate) == stable_signature:
                    matched_message = candidate
                    break
            if matched_message is not None:
                materials = materials if isinstance(materials, list) else self._load_material_outreach_materials()
                matched_material = None
                retained = []
                for item in materials or []:
                    if not isinstance(item, dict):
                        continue
                    item_source = str(item.get("source") or "").strip()
                    item_id = str(item.get("id") or "").strip()
                    item_signature = str(item.get("stable_signature") or "").strip()
                    if item_source == source and (
                        (stable_signature and item_signature == stable_signature)
                        or (material_id and item_id == material_id)
                    ):
                        matched_material = item
                        continue
                    retained.append(item)
                refreshed_entry = build_material_entry(
                    material_id or f"mat_{uuid.uuid4().hex}",
                    source,
                    matched_message,
                )
                if matched_material:
                    refreshed_entry["id"] = str(matched_material.get("id") or refreshed_entry.get("id") or "").strip()
                    for field in ("ownership", "copy_note", "status", "forward_test_status", "last_error"):
                        if field in matched_material:
                            refreshed_entry[field] = matched_material.get(field)
                retained.append(refreshed_entry)
                materials = trim_material_pool_by_source(
                    retained,
                    limit_map=getattr(self.config, "material_source_pool_limit_map", {}) or {},
                )
                refreshed_id = str(refreshed_entry.get("id") or "").strip()
                if refreshed_id:
                    self._material_runtime_messages[refreshed_id] = matched_message
                self._save_material_outreach_materials(materials)
                read_strategy = self._material_source_read_strategy(source)
                log(
                    message=(
                        f"[素材转发] 已定向刷新素材句柄：来源 {source}，"
                        f"素材 {refreshed_id or stable_signature}，读取方案：{read_strategy}"
                    )
                )
            else:
                materials = self._rebuild_material_pool_for_source(source)
        elif source:
            materials = self._rebuild_material_pool_for_source(source)
        else:
            materials = materials if isinstance(materials, list) else self._load_material_outreach_materials()
        refreshed = None
        for item in materials or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("status", "active") or "").strip() != "active":
                continue
            if stable_signature and str(item.get("stable_signature") or "").strip() == stable_signature:
                refreshed = item
                break
            if material_id and str(item.get("id") or "").strip() == material_id:
                refreshed = item
                break
        refreshed = refreshed or material
        refreshed_id = str(refreshed.get("id") or material_id).strip()
        message = (getattr(self, "_material_runtime_messages", {}) or {}).get(refreshed_id)
        return refreshed, message, materials

    def _material_runtime_message(self, material, materials=None, *, refresh_missing=True):
        material = material if isinstance(material, dict) else {}
        material_id = str(material.get("id") or "").strip()
        message = (getattr(self, "_material_runtime_messages", {}) or {}).get(material_id)
        if message is not None or not refresh_missing:
            return material, message, materials
        return self._refresh_material_runtime_message(material, materials)

    def _forward_material_message(
        self,
        message,
        targets,
        *,
        preface="",
        material_source="",
        material_type="",
        material_title="",
        echo_source="material_outreach",
        delivery_id="",
        request_id="",
        run_id="",
        batch_id="",
    ):
        material_source = str(material_source or "").strip()
        if material_source:
            with self._get_material_source_read_lock(material_source):
                return self._forward_material_message_unlocked(
                    message,
                    targets,
                    preface=preface,
                    material_source=material_source,
                    material_type=material_type,
                    material_title=material_title,
                    echo_source=echo_source,
                    delivery_id=delivery_id,
                    request_id=request_id,
                    run_id=run_id,
                    batch_id=batch_id,
                )
        return self._forward_material_message_unlocked(
            message,
            targets,
            preface=preface,
            material_source=material_source,
            material_type=material_type,
            material_title=material_title,
            echo_source=echo_source,
            delivery_id=delivery_id,
            request_id=request_id,
            run_id=run_id,
            batch_id=batch_id,
        )

    def _forward_material_message_unlocked(
        self,
        message,
        targets,
        *,
        preface="",
        material_source="",
        material_type="",
        material_title="",
        echo_source="material_outreach",
        delivery_id="",
        request_id="",
        run_id="",
        batch_id="",
    ):
        target_label = "、".join(str(item or "").strip() for item in (targets or []) if str(item or "").strip())
        echo_expectations = self._remember_material_outbound_echoes(
            targets,
            material_type or getattr(message, "type", ""),
            preface=preface,
            material_title=material_title or getattr(message, "content", ""),
            source=echo_source,
        )
        try:
            with warn_slow_wechat_ui_action(f"message.forward({target_label or 'unknown'})"):
                result = self._ui_forward_message(
                    OwnedChat(
                        self._ui_owner,
                        material_source,
                        self._material_source_chat_type(material_source),
                    ),
                    message,
                    targets,
                    preface=preface,
                    delivery_id=delivery_id,
                    request_id=request_id,
                    run_id=run_id,
                    batch_id=batch_id,
                    echo_delivery_ids=[
                        expectation["delivery_id"]
                        for expectation in echo_expectations
                    ],
                )
        except wechat_ui_actions.DeliveryAlreadySubmitted:
            for expectation in echo_expectations:
                self._reply_echo_tracker.discard(expectation["delivery_id"])
            raise
        except wechat_ui_actions.IntentCancelled:
            for expectation in echo_expectations:
                self._reply_echo_tracker.discard(expectation["delivery_id"])
            return False, "素材转发任务已更新或取消"
        success, result_error = is_forward_result_success(result)
        if success:
            store = getattr(self, "_message_store", None)
            for expectation in echo_expectations:
                if store is not None:
                    store.append_confirmed_outbound_once(
                        expectation["delivery_id"],
                        expectation["target"],
                        content=expectation["action"].content,
                        sent_at=time.time(),
                        message_type=expectation["message_type"],
                        metadata={"source": echo_source},
                    )
        return success, result_error

    def _remember_material_outbound_echoes(
        self,
        targets,
        material_type="",
        *,
        preface="",
        material_title="",
        source="material_outreach",
    ):
        kind = str(material_type or "").strip().lower() or "unknown"
        preface = str(preface or "").strip()
        material_title = str(material_title or "").strip()
        tracker = getattr(self, "_reply_echo_tracker", None)
        if tracker is None:
            tracker = ReplyEchoTracker()
            self._reply_echo_tracker = tracker
        echo_group_id = uuid.uuid4().hex
        expectations = []
        for target in targets or []:
            target = str(target or "").strip()
            if not target:
                continue
            if preface:
                delivery_id = f"forward_{echo_group_id}:{len(expectations)}"
                action = ReplyAction(ReplyKind.TEXT, preface, ReplySource.AI)
                tracker.reserve(
                    delivery_id,
                    target,
                    action,
                    confirmable=False,
                    chat_type="private",
                )
                expectations.append({
                    "delivery_id": delivery_id,
                    "target": target,
                    "action": action,
                    "message_type": "text",
                })
            delivery_id = f"forward_{echo_group_id}:{len(expectations)}"
            action = ReplyAction(
                ReplyKind.FILE,
                material_title or "[素材]",
                ReplySource.AI,
            )
            tracker.reserve(
                delivery_id,
                target,
                action,
                confirmable=False,
                chat_type="private",
                message_types=(kind,) if kind != "unknown" else (),
            )
            expectations.append({
                "delivery_id": delivery_id,
                "target": target,
                "action": action,
                "message_type": kind if kind != "unknown" else "file",
            })
        return expectations

    def _append_material_outreach_preface_progress(self, record, status, *, reason="", detail="", now=None):
        now = now or datetime.now()
        progress = build_progress_record(
            str(record.get("run_id") or "").strip(),
            str(record.get("task_id") or "").strip(),
            {
                "contact_key": str(record.get("contact_key") or "").strip(),
                "send_name": str(record.get("target") or "").strip(),
                "display_name": str(record.get("display_name") or record.get("target") or "").strip(),
                "warnings": [],
            },
            status,
            reason=reason,
            detail=detail,
            now=now,
        )
        self._append_material_progress_records([progress], limit=1000)

    def _skip_material_outreach_preface_record(self, record, detail, *, now=None):
        now = now or datetime.now()
        skip = build_skip_record(
            record.get("task_id"),
            record.get("target"),
            "ai_preface_failed",
            detail,
            now=now,
            material_title=record.get("material_title", ""),
            material_type=record.get("material_type", ""),
            material_id=record.get("material_id", ""),
        )
        self._append_material_skip_record(skip, limit=1000)
        self._append_material_outreach_preface_progress(
            record,
            "skipped",
            reason="ai_preface_failed",
            detail=detail,
            now=now,
        )
        record["status"] = "failed"
        record["error"] = str(detail or "")
        record["finished_at"] = now.replace(microsecond=0).isoformat()
        return True

    def _send_material_outreach_preface_record(self, record, *, now=None):
        now = now or datetime.now()
        materials = self._load_material_outreach_materials()
        material = self._find_material_outreach_material_for_queue(record, materials)
        if material is None and record.get("material_source"):
            self._rebuild_material_pool_for_source(record.get("material_source"))
            materials = self._load_material_outreach_materials()
            material = self._find_material_outreach_material_for_queue(record, materials)
        if material is None:
            error = "没有可用素材"
            self._append_material_send_record(
                build_send_record(
                    record.get("task_id"),
                    record.get("material_id"),
                    record.get("material_type"),
                    record.get("target"),
                    False,
                    now=now,
                    error=error,
                    preface="",
                    material_title=record.get("material_title", ""),
                    material_source=record.get("material_source", ""),
                    stable_signature=record.get("stable_signature", ""),
                    task_name=record.get("task_name", ""),
                    batch_id=record.get("batch_id") or record.get("run_id") or record.get("queue_id") or record.get("task_id"),
                    run_id=record.get("run_id", ""),
                    targets_summary=record.get("targets_summary", ""),
                    content_summary=record.get("preface") or record.get("content_summary", ""),
                    media_summary=record.get("media_summary", ""),
                    material_summary=record.get("material_summary", ""),
                    raw_targets=record.get("raw_targets"),
                    raw_messages=record.get("raw_messages"),
                    raw_media=record.get("raw_media"),
                    raw_material=record.get("raw_material"),
                ),
                limit=1000,
            )
            self._append_material_outreach_preface_progress(record, "failed", detail=error, now=now)
            record["status"] = "failed"
            record["error"] = error
            record["finished_at"] = now.replace(microsecond=0).isoformat()
            return False

        if record.get("preface_status") != "success" and record.get("failure_mode") == "skip_target":
            detail = str(record.get("preface_error") or "AI 附加文案未准备完成").strip()
            return self._skip_material_outreach_preface_record(record, detail, now=now)

        material_id = str(material.get("id") or record.get("material_id") or "").strip()
        title = material_title(material) or record.get("material_title", "")
        material, message, materials = self._material_runtime_message(material, materials, refresh_missing=True)
        material_id = str(material.get("id") or material_id).strip()
        title = material_title(material) or title
        if message is None:
            error = "素材运行时句柄不存在"
            self._append_material_send_record(
                build_send_record(
                    record.get("task_id"),
                    material_id,
                    material.get("type_bucket") or material.get("type") or record.get("material_type"),
                    record.get("target"),
                    False,
                    now=now,
                    error=error,
                    preface="",
                    material_title=title,
                    material_source=material.get("source") or record.get("material_source", ""),
                    stable_signature=record.get("stable_signature", ""),
                    task_name=record.get("task_name", ""),
                    batch_id=record.get("batch_id") or record.get("run_id") or record.get("queue_id") or record.get("task_id"),
                    run_id=record.get("run_id", ""),
                    targets_summary=record.get("targets_summary", ""),
                    content_summary=record.get("preface") or record.get("content_summary", ""),
                    media_summary=record.get("media_summary", ""),
                    material_summary=record.get("material_summary", ""),
                    raw_targets=record.get("raw_targets"),
                    raw_messages=record.get("raw_messages"),
                    raw_media=record.get("raw_media"),
                    raw_material=record.get("raw_material"),
                ),
                limit=1000,
            )
            self._append_material_outreach_preface_progress(record, "failed", detail=error, now=now)
            record["status"] = "failed"
            record["error"] = error
            record["finished_at"] = now.replace(microsecond=0).isoformat()
            return False

        preface = str(record.get("preface") or "").strip() if record.get("preface_status") == "success" else ""
        success = False
        error = ""
        try:
            success, error = self._forward_material_message(
                message,
                [record.get("target")],
                preface=preface,
                material_source=material.get("source") or record.get("material_source", ""),
                material_type=material.get("type_bucket") or material.get("type") or record.get("material_type"),
                material_title=title,
            )
            if not success and self._material_forward_error_needs_refresh(error):
                log(level="WARNING", message=f"[素材转发] 素材句柄失效，已刷新来源子窗口后重试：{title}")
                material, message, materials = self._refresh_material_runtime_message(material, materials)
                material_id = str(material.get("id") or material_id).strip()
                title = material_title(material) or title
                if message is not None:
                    success, error = self._forward_material_message(
                        message,
                        [record.get("target")],
                        preface=preface,
                        material_source=material.get("source") or record.get("material_source", ""),
                        material_type=material.get("type_bucket") or material.get("type") or record.get("material_type"),
                        material_title=title,
                    )
        except Exception as exc:
            error = str(exc)
            if self._material_forward_error_needs_refresh(error):
                try:
                    log(level="WARNING", message=f"[素材转发] 素材句柄异常失效，已刷新来源子窗口后重试：{title}")
                    material, message, materials = self._refresh_material_runtime_message(material, materials)
                    material_id = str(material.get("id") or material_id).strip()
                    title = material_title(material) or title
                    if message is not None:
                        success, error = self._forward_material_message(
                            message,
                            [record.get("target")],
                            preface=preface,
                            material_source=material.get("source") or record.get("material_source", ""),
                            material_type=material.get("type_bucket") or material.get("type") or record.get("material_type"),
                            material_title=title,
                        )
                except Exception as retry_exc:
                    error = str(retry_exc)
        finally:
            if material.get("source"):
                self._restore_material_source_position(material.get("source"))

        self._append_material_send_record(
            build_send_record(
                record.get("task_id"),
                material_id,
                material.get("type_bucket") or material.get("type") or record.get("material_type"),
                record.get("target"),
                success,
                now=now,
                error=error,
                preface=preface,
                material_title=title,
                material_source=material.get("source") or record.get("material_source", ""),
                stable_signature=record.get("stable_signature", ""),
                task_name=record.get("task_name", ""),
                batch_id=record.get("batch_id") or record.get("run_id") or record.get("queue_id") or record.get("task_id"),
                run_id=record.get("run_id", ""),
                targets_summary=record.get("targets_summary", ""),
                content_summary=preface or record.get("content_summary", ""),
                media_summary=record.get("media_summary", ""),
                material_summary=record.get("material_summary", ""),
                raw_targets=record.get("raw_targets"),
                raw_messages=([{"type": "text", "text": preface}] if preface else record.get("raw_messages")),
                raw_media=record.get("raw_media"),
                raw_material=record.get("raw_material"),
            ),
            limit=1000,
        )
        self._append_material_outreach_preface_progress(
            record,
            "success" if success else "failed",
            detail=error,
            now=now,
        )
        material["forward_test_status"] = "success" if success else "failed"
        material["last_error"] = "" if success else error
        self._save_material_outreach_materials(materials)
        record["status"] = "sent" if success else "failed"
        record["error"] = str(error or "")
        record["finished_at"] = now.replace(microsecond=0).isoformat()
        return success

    def _process_material_outreach_preface_queue(self, now=None):
        if self.is_stop_requested():
            return False
        now = now or datetime.now()
        with self._material_outreach_runtime_lock():
            queue_records = self._load_material_outreach_preface_queue()
            changed = False
            for record in due_prefetch_records(queue_records, now=now):
                try:
                    material = self._find_material_outreach_material_for_queue(
                        record,
                        self._load_material_outreach_materials(),
                    ) or {
                        "content_preview": record.get("material_title", ""),
                        "type_bucket": record.get("material_type", ""),
                        "source": record.get("material_source", ""),
                        "ownership": record.get("material_ownership", ""),
                        "copy_note": record.get("material_copy_note", ""),
                    }
                    preface = self._generate_material_outreach_ai_preface(
                        {
                            "task_id": record.get("task_id"),
                            "task_name": record.get("task_name"),
                            "ai_preface_goal": record.get("ai_preface_goal", ""),
                            "ai_preface_intensity": record.get("ai_preface_intensity", ""),
                            "ai_preface_extra_instruction": record.get("ai_preface_extra_instruction", ""),
                        },
                        record.get("target"),
                        material,
                    )
                    mark_preface_generated(record, preface, now=now)
                except Exception as exc:
                    mark_preface_failed(record, exc, now=now)
                changed = True
            for record in due_send_records(queue_records, now=now):
                if self.is_stop_requested():
                    break
                self._send_material_outreach_preface_record(record, now=now)
                changed = True
            if changed:
                resolved_cycles = set()
                for record in queue_records:
                    status = str((record or {}).get("status") or "").strip()
                    if status not in {"sent", "failed"}:
                        continue
                    cycle_key = (
                        str(record.get("task_id") or "").strip(),
                        str(record.get("run_id") or "").strip(),
                        str(record.get("scheduled_at") or "").strip(),
                    )
                    if cycle_key in resolved_cycles:
                        continue
                    resolved_cycles.add(cycle_key)
                    self._resolve_material_outreach_preface_cycle(
                        cycle_key[0],
                        run_id=cycle_key[1],
                        scheduled_at=cycle_key[2],
                        success_hint=(status == "sent"),
                        now=now,
                        cycle_records=queue_records,
                    )
                queue_records = [
                    record
                    for record in queue_records
                    if str((record or {}).get("status") or "").strip() == "pending"
                ]
                self._save_material_outreach_preface_queue(queue_records)
            return changed

    def _send_material_outreach_action(self, task, action, materials):
        material = action["material"]
        targets = action.get("targets") or [action.get("target")]
        targets = [target for target in targets if target]
        target_label = "、".join(targets)
        material_id = material.get("id")
        title = material_title(material)
        material_type = material.get("type_bucket") or material.get("type")
        material_source = material.get("source", "")
        snapshot = task.get("_outreach_target_snapshot") if isinstance(task, dict) else None
        action_mode = str(action.get("mode") or "").strip()
        preface = str(action.get("preface") or "")
        if action_mode == "ai_preface":
            # 预生成 AI 附加文案，发送时跟随素材转发，不单独提前发送。
            return self._queue_material_outreach_preface_action(task, action)
        if not preface:
            preface_config = normalize_material_outreach_preface_config(task)
            if preface_config.get("preface_mode") == "custom":
                preface = build_custom_material_preface(
                    preface_config.get("preface_text", ""),
                    random_emojis=preface_config.get("preface_random_emojis", False),
                )
        material, message, materials = self._material_runtime_message(material, materials, refresh_missing=True)
        material_id = material.get("id")
        title = material_title(material)
        material_type = material.get("type_bucket") or material.get("type")
        material_source = material.get("source", "")
        if message is None:
            for target in targets:
                self._append_material_skip_record(
                    build_skip_record(
                        task.get("task_id"),
                        target,
                        "stale",
                        "素材运行时句柄不存在",
                        material_title=title,
                        material_type=material_type,
                        material_id=material_id,
                        batch_id=(snapshot or {}).get("batch_id", ""),
                        run_id=(snapshot or {}).get("run_id", ""),
                        targets_summary=(snapshot or {}).get("targets_summary", ""),
                        content_summary=preface,
                        material_summary="{}：{}".format(material_type, title).strip("："),
                        raw_targets=(snapshot or {}).get("raw_targets"),
                        raw_messages=[{"type": "text", "text": preface}] if preface else [],
                        raw_material={
                            "material_id": material_id,
                            "title": title,
                            "type": material_type,
                            "source": material_source,
                            "ownership": material.get("ownership", ""),
                            "copy_note": material.get("copy_note", ""),
                        },
                    ),
                    limit=1000,
                )
            return False
        success = False
        error = ""
        uncertain = False
        run_id = str((snapshot or {}).get("run_id") or "").strip()
        request_id = run_id or str(task.get("request_id") or task.get("task_id") or "").strip()
        delivery_seed = json.dumps(
            [request_id, str(material_id or ""), targets, preface],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        batch_id = f"outreach_{hashlib.sha256(delivery_seed.encode('utf-8')).hexdigest()[:24]}"
        if snapshot:
            self._update_material_progress_records_for_send(
                snapshot,
                targets,
                status="inflight",
                now=datetime.now(),
                limit=1000,
            )
        try:
            success, error = self._forward_material_message(
                message,
                targets,
                preface=preface,
                material_source=material_source,
                material_type=material_type,
                material_title=title,
                delivery_id=batch_id,
                request_id=request_id,
                run_id=run_id,
                batch_id=batch_id,
            )
        except Exception as exc:
            error = str(exc)
            uncertain = True
        finally:
            if material_source:
                self._restore_material_source_position(material_source)
        for target in targets:
            self._append_material_send_record(
                build_send_record(
                    task.get("task_id"),
                    material_id,
                    material.get("type_bucket") or material.get("type"),
                    target,
                    success,
                    error=error,
                    preface=preface,
                    material_title=title,
                    material_source=material_source,
                    batch_id=batch_id,
                    run_id=(snapshot or {}).get("run_id", ""),
                    targets_summary=(snapshot or {}).get("targets_summary", ""),
                    content_summary=preface,
                    material_summary="{}：{}".format(material_type, title).strip("："),
                    raw_targets=(snapshot or {}).get("raw_targets"),
                    raw_messages=[{"type": "text", "text": preface}] if preface else [],
                    raw_material={
                        "material_id": material_id,
                        "title": title,
                        "type": material_type,
                        "source": material_source,
                        "ownership": material.get("ownership", ""),
                        "copy_note": material.get("copy_note", ""),
                    },
                    status="uncertain" if uncertain else "",
                ),
                limit=1000,
            )
        if snapshot:
            self._update_material_progress_records_for_send(
                snapshot,
                targets,
                success=success,
                status="uncertain" if uncertain else "",
                error=error,
                now=datetime.now(),
                limit=1000,
            )
        material["forward_test_status"] = "success" if success else "failed"
        material["last_error"] = "" if success else error
        self._save_material_outreach_materials(materials)
        if uncertain:
            log(
                level="WARNING",
                message=f"[素材转发] {material_id} -> {target_label} 结果待核实，不会自动重试：{error}",
            )
            return {"status": "uncertain", "message": error, "targets": targets}
        log(
            level="INFO" if success else "WARNING",
            message=f"[素材转发] {material_id} -> {target_label}，附带文案：{preface or '无'}，成功：{success}，错误：{error}",
        )
        return success

    def _check_random_material_outreach(self):
        now = datetime.now()
        today = now.date()
        for task in iter_enabled_material_outreach_tasks(self.config.material_outreach_list):
            if task["mode"] != "random":
                continue
            task_id = task.get("task_id")
            if not task_id:
                continue
            state = self._random_material_outreach_state.setdefault(task_id, {
                "next_fire": None,
                "last_fire_date": None,
                "week_cache": None,
                "month_cache": None,
            })
            if not prepare_random_material_outreach_day(
                task_id,
                task,
                state,
                today,
                log_info=lambda message: log(level="DEBUG", message=message),
            ):
                continue
            task = dict(task)
            task["time_start"], task["time_end"] = material_random_time_window(
                self.config.config.get("everyday_start_stop_bot_switch", False),
                self.config.config.get("everyday_start_bot_time", "08:00"),
                self.config.config.get("everyday_stop_bot_time", "23:00"),
                now=now,
            )
            if not task["time_start"] or not task["time_end"]:
                state["next_fire"] = None
                continue
            if state["next_fire"] is None:
                plan_random_material_outreach_fire_time(
                    task_id,
                    task,
                    state,
                    now,
                    log_info=lambda message: log(message=message),
                )
            trigger_random_material_outreach_if_due(
                task_id,
                task,
                state,
                now,
                send_material_outreach=self.send_material_outreach,
                log_info=lambda message: log(message=message),
                log_error=lambda message: log(level="ERROR", message=message),
            )

    # ----------------------------------------------------------
    # 消息回调与处理入口
    # ----------------------------------------------------------

    def message_handle_callback(self, msg, chat):
        """
        wxautox 监听器的消息回调函数。
        每当监听到新消息时由 wxautox 自动调用。

        :param msg:  消息对象（含 type、attr、sender、content 等属性）
        :param chat: 聊天窗口子对象（含 who 等属性）
        """
        if self.is_stop_requested():
            return True
        if getattr(msg, "_wxbot_startup_recovery", False):
            message_routing.prepare_message_media(self, msg, chat)
            return self.process_message(chat, msg)
        try:
            received_at = getattr(msg, "_wxbot_received_at", None) or datetime.now()
            self._last_incoming_message_at = time.time()
            setattr(msg, "_wxbot_ingress_source", "subwindow")
            setattr(msg, "_wxbot_received_at", received_at)
            message_routing.record_runtime_inbound_event(
                self,
                msg,
                getattr(chat, "chat_type", ""),
            )
            msg_type_label = {
                "text": "文本",
                "voice": "语音",
                "image": "图片",
                "video": "视频",
                "file": "文件",
            }.get(str(getattr(msg, "type", "") or "").lower(), str(getattr(msg, "type", "") or "未知"))
            is_group = str(getattr(chat, "chat_type", "") or "").lower() == "group"
            is_private = not is_group and getattr(msg, "attr", "") == "friend"
            scene_label = "群聊" if is_group else "私聊" if is_private else "消息"
            text = f"{scene_label} {chat.who}：收到{msg_type_label}消息"
            if is_group or not is_private:
                text += f"，发送人：{msg.sender}"
            if str(getattr(msg, "type", "") or "").lower() in {"text", "voice", "quote", "link"}:
                content_preview = re.sub(r"\s+", " ", str(getattr(msg, "content", "") or "")).strip()
                if len(content_preview) > 80:
                    content_preview = content_preview[:79].rstrip() + "…"
                if content_preview:
                    text += f"，内容：{content_preview}"
            if getattr(msg, "attr", "") not in {"self", "system"}:
                log(message=text)
            callback_result = None

            message_routing.prepare_message_media(self, msg, chat)

            inbound_direction = str(
                getattr(msg, "_wxbot_inbound_direction", "") or ""
            ).strip()
            if inbound_direction == "friend" or (
                not inbound_direction and msg.attr == "friend"
            ):
                callback_result = message_routing.handle_friend_message_callback(self, msg, chat, text=text)

            elif inbound_direction == "system" or (
                not inbound_direction and msg.attr == "system"
            ):
                # 系统消息：触发群新人欢迎语逻辑（仅限已配置群组，纯转发来源群组跳过）
                if (
                    getattr(chat, "chat_type", "private") == "group"
                    and self.config.group_switch
                    and self.config.group_welcome
                    and chat.who in self.config.group
                ):
                    result = self.send_group_welcome_msg(chat, msg)
                    if not result:
                        self.is_err(
                            self.wx.nickname + f" wxbot发送群新人欢迎语失败！",
                            text + '\n' + self._result_error_text(result),
                        )

            elif msg.attr == "self":
                if self._handle_material_source_message(chat, msg):
                    return True

            ordinary_private_self = self._is_ordinary_private_self_message(chat, msg)
            self._mark_chat_memory_dirty(chat, msg)
            if ordinary_private_self:
                self._handle_private_self_message_boundary(chat, msg)
            if callback_result is not None:
                return callback_result
        except Exception as e:
            if self._reply_event_ids(msg):
                raise
            # 回调函数出现未捕获异常时标记 callback_is_die，由主循环检测并处理
            if self._arm_listener_auto_recovery(e, source="消息回调"):
                return
            self.callback_is_die = True
            self.is_err(self.wx.nickname + " wxbot回调函数处理出错！处理监听失败！！", e)

    def _is_ordinary_private_self_message(self, chat, message):
        return (
            getattr(message, "attr", "") == "self"
            and getattr(chat, "chat_type", "private") != "group"
            and getattr(message, "_wxbot_inbound_direction", "") != "bot_echo"
        )

    def _invalidate_private_ai_reply_turn(self, chat_name):
        name = str(chat_name or "").strip()
        if not name:
            return {}
        sequence = self._get_private_message_sequence(name)
        pipeline = self._clear_private_message_pipeline(name)
        discarded_messages = []
        if isinstance(pipeline, dict):
            discarded_messages.extend(pipeline.get("open_messages") or [])
            for batch in pipeline.get("queued_batches") or ():
                discarded_messages.extend(batch or [])
        for message in discarded_messages:
            self._cancel_failed_reply_attempt(message, "manual reply took over the conversation")
        with self._chat_merge_lock:
            cancelled_voice = self._cancel_pending_private_voice_transcription_locked(name)
        return {
            "sequence": sequence,
            "cancelled_voice_transcription": cancelled_voice,
        }

    def _handle_private_self_message_boundary(self, chat, message=None):
        if not self._is_ordinary_private_self_message(
            chat,
            message or SimpleNamespace(attr="self"),
        ):
            return False
        name = str(getattr(chat, "who", "") or "").strip()
        if not name:
            return False
        self._ensure_message_runtime_state()
        with self._chat_merge_lock:
            pipeline = self._private_message_pipelines.get(name)
            has_open_batch = isinstance(pipeline, dict) and bool(pipeline.get("open_messages"))
            has_committed_batch = isinstance(pipeline, dict) and (
                bool(pipeline.get("queued_batches"))
                or bool(pipeline.get("worker_running"))
            )
            has_voice_transcription = name in (getattr(self, "_pending_private_voice_transcription", {}) or {})
        if has_committed_batch:
            self._invalidate_private_ai_reply_turn(name)
            try:
                log(level="DEBUG", message=f"运行事件：人工介入已确认 runtime_id={self._runtime_instance_id}")
            except Exception:
                pass
            log(message=f"私聊 {name}：检测到人工发送的消息，已停止当前 AI 回复")
            return True

        if has_open_batch or has_voice_transcription:
            self._invalidate_private_ai_reply_turn(name)
            try:
                log(level="DEBUG", message=f"运行事件：人工介入已确认 runtime_id={self._runtime_instance_id}")
            except Exception:
                pass
            log(message=f"私聊 {name}：检测到人工发送的消息，已取消此前待回复内容")
        else:
            log(message=f"私聊 {name}：检测到人工发送的消息，已记入聊天记录")
        return True

    def _private_reply_can_continue(self, chat, *, log_prefix="私聊", expected_sequence=None):
        target = str(getattr(chat, "who", "") or "").strip()
        if self.is_stop_requested():
            if target:
                log(message=f"{log_prefix} {target}：机器人正在停止，已停止 AI 回复")
            return False
        if self._pause_chat_reply:
            if target:
                log(message=f"{log_prefix} {target}：私聊自动回复已暂停，已停止 AI 回复")
            return False
        if getattr(self.config, "chat_listen_only", False):
            if target:
                log(message=f"{log_prefix} {target} 已启用只监听不AI回复，跳过 AI 调用")
            return False
        if expected_sequence is not None and target:
            try:
                expected_sequence = int(expected_sequence)
            except Exception:
                expected_sequence = None
            if expected_sequence is not None and self._get_private_message_sequence(target) != expected_sequence:
                return False
        return True

    @staticmethod
    def _reply_event_ids(message):
        event_ids = []
        for event_id in getattr(message, "_wxbot_event_ids", ()) or ():
            event_id = str(event_id or "").strip()
            if event_id and event_id not in event_ids:
                event_ids.append(event_id)
        if not event_ids:
            event_id = str(getattr(message, "_wxbot_event_id", "") or "").strip()
            if event_id:
                event_ids.append(event_id)
        return tuple(event_ids)

    def _ensure_reply_job(self, chat, message, *, chat_type="private", route_source=""):
        store = getattr(self, "_message_store", None)
        event_ids = self._reply_event_ids(message)
        if store is None or not event_ids:
            return ""
        conversation = str(getattr(chat, "who", "") or "").strip()
        route_source = str(
            route_source
            or getattr(message, "_wxbot_reply_route_source", "")
            or getattr(message, "_wxbot_recovery_route_source", "")
            or ""
        ).strip()
        raw_expected_version = getattr(message, "_wxbot_event_version", None)
        if raw_expected_version is None:
            expected_version = store.conversation_version(conversation, chat_type=chat_type)
        else:
            expected_version = int(raw_expected_version)
        expires_at = float(getattr(message, "_wxbot_reply_expires_at", 0.0) or 0.0)
        if expires_at <= 0:
            expires_at = self._received_timestamp(getattr(message, "_wxbot_received_at", 0.0)) + DEFAULT_REPLY_TTL_SECONDS
        turn_id = str(getattr(message, "_wxbot_recovery_turn_id", "") or "").strip()
        if not turn_id:
            identity = json.dumps(
                ["wxbot-reply-v1", chat_type, conversation, event_ids],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            turn_id = "turn_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        store.create_reply_job(
            turn_id,
            conversation=conversation,
            chat_type=chat_type,
            expected_version=expected_version,
            expires_at=expires_at,
            event_ids=event_ids,
            route_source=route_source,
        )
        message._wxbot_reply_turn_id = turn_id
        message._wxbot_event_version = expected_version
        message._wxbot_reply_expires_at = expires_at
        message._wxbot_reply_route_source = route_source
        return turn_id

    def _cancel_unfinished_reply_job(self, message, reason, *, status="cancelled"):
        turn_id = str(getattr(message, "_wxbot_reply_turn_id", "") or "").strip()
        store = getattr(self, "_message_store", None)
        if not turn_id or store is None:
            return
        job = store.get_reply_job(turn_id)
        if job and job.get("status") in {"pending", "generating"}:
            store.cancel_pending(turn_id, status, reason)

    def _reply_job_can_generate(self, chat, message, *, chat_type, route_source):
        if self.is_stop_requested():
            self._cancel_failed_reply_attempt(message, "robot stopped before reply generation")
            return False
        turn_id = self._ensure_reply_job(
            chat,
            message,
            chat_type=chat_type,
            route_source=route_source,
        )
        if not turn_id:
            return True
        if self.is_stop_requested():
            self._cancel_unfinished_reply_job(message, "robot stopped before reply generation")
            return False
        return self._message_store.mark_reply_job_generating(turn_id) == "generating"

    def _reply_retry_delay(self, message, exc):
        if not is_retryable_sqlite_error(exc):
            return None
        if self.is_stop_requested():
            return None
        expires_at = float(getattr(message, "_wxbot_reply_expires_at", 0.0) or 0.0)
        attempts = int(getattr(message, "_wxbot_business_retry_count", 0) or 0)
        remaining = expires_at - time.time()
        if expires_at <= 0 or remaining <= 0:
            return None
        message._wxbot_business_retry_count = attempts + 1
        retry_delays = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
        return min(retry_delays[min(attempts, len(retry_delays) - 1)], remaining)

    def _cancel_failed_reply_attempt(self, message, reason):
        try:
            turn_id = str(getattr(message, "_wxbot_reply_turn_id", "") or "").strip()
            if turn_id:
                self._cancel_unfinished_reply_job(message, reason)
                return ""
            store = getattr(self, "_message_store", None)
            event_ids = self._reply_event_ids(message)
            if store is not None and event_ids:
                store.mark_inbound_events(event_ids, "cancelled")
            return ""
        except Exception as exc:
            return str(exc)

    def _mark_inbound_no_reply(self, message):
        event_ids = self._reply_event_ids(message)
        store = getattr(self, "_message_store", None)
        if store is not None and event_ids:
            store.mark_inbound_events(event_ids, "handled")

    @staticmethod
    def _recovery_route_matches(message, route_source):
        expected = str(
            getattr(message, "_wxbot_recovery_route_source", "") or ""
        ).strip()
        return not expected or expected == route_source

    def wx_send_ai(self, chat, message):
        if not self._private_reply_can_continue(chat):
            if self.is_stop_requested():
                return True
            if getattr(message, "_wxbot_reply_turn_id", ""):
                self._cancel_unfinished_reply_job(message, "private reply is paused or disabled")
            else:
                self._mark_inbound_no_reply(message)
            return True
        user_key = self._get_reply_count_key(chat, message)
        message_type = str(getattr(message, "type", "") or "").strip().lower()
        message_body = strip_message_shell(getattr(message, "content", ""), message_type)
        keyword_plan = plan_private_keyword_reply(
            bool(getattr(self.config, "chat_keyword_switch", False)),
            self.config.keyword_dict,
            message_body,
        )
        limit_reached = self._text_reply_limit_reached(user_key, chat_type="private")
        route_source = (
            "private_limit"
            if limit_reached
            else "private_keyword"
            if keyword_plan
            else "private_ai"
        )
        if not self._recovery_route_matches(message, route_source):
            self._cancel_unfinished_reply_job(message, "reply route changed during restart")
            return True
        message._wxbot_reply_route_source = route_source
        if not self._reply_job_can_generate(
            chat,
            message,
            chat_type="private",
            route_source=route_source,
        ):
            return True
        if limit_reached:
            handled, result = self._check_text_reply_limit_runtime(
                chat,
                user_key,
                message=message,
                chat_type="private",
            )
            if not handled:
                self._cancel_unfinished_reply_job(message, "reply limit changed during routing")
                return True
            self._cancel_unfinished_reply_job(message, "reply limit routing completed without delivery")
            return result
        result = self._wx_send_ai_once(
            chat,
            message,
            keyword_plan=keyword_plan,
            user_key=user_key,
        )
        self._cancel_unfinished_reply_job(message, "routing completed without a delivery")
        return result

    def _wx_send_ai_once(self, chat, message, *, keyword_plan, user_key):
        """私聊 AI 自动回复。连续消息按好友串行处理，安全状态变化会停止发送。"""
        reply_message_sequence = self._get_private_message_sequence(chat.who)
        result = True

        api_error_reply = False
        api_error_should_mark = False
        preprocess_fallback_should_mark = False
        voice_candidate = False
        image_reply_context_used = False
        self._voice_reply_state = (
            getattr(self, "_voice_reply_state", None)
            or load_voice_reply_state(self._voice_reply_state_path())
        )
        try:
            message_type = str(getattr(message, "type", "") or "").strip().lower()
            message_body = strip_message_shell(getattr(message, "content", ""), message_type)
            message_semantic_text = format_message_semantic_text(message)
            model_message_text = format_model_message_text(message)
            if keyword_plan:
                log(message=f"私聊 {chat.who} 关键字消息：" + message_body)
                reply_actions = normalize_keyword_reply_actions(keyword_plan["reply"])
                send_success, result = self._send_keyword_reply_actions(
                    chat,
                    reply_actions,
                    message=message,
                )
                if send_success and getattr(self.config, "chat_text_reply_limit_switch", False) and user_key:
                    self.reply_count_store.increment_ai_count(
                        user_key,
                        limit_hours=getattr(self.config, "chat_text_reply_limit_hours", 5),
                    )
                if send_success:
                    self._record_reply_metric_success(chat.who, chat_type="private")
                    self._record_keyword_reply_success(chat.who, chat_type="private", action_count=len(reply_actions))
                return result
            else:
                history = []
                if self.config.memory_switch and self.memory_manager:
                    repaired_history = self._repair_context_before_ai(
                        chat,
                        message,
                        chat_type="private",
                    )
                    if self.config.memory_context_switch:
                        history = self._get_model_context_history(
                            str(getattr(chat, "who", "") or "").strip(),
                            event_ids=self._reply_event_ids(message),
                            chat_type="private",
                            extra_messages=repaired_history,
                        )
                voice_candidate = private_voice_candidate(self.config, message)
                if getattr(message, "attr", "") == "friend":
                    if (
                        getattr(message, "type", "") in {"text", "voice", "image"}
                        and self._current_ai_material_outreach_config().get("ai_material_outreach_switch")
                    ):
                        detection_now = datetime.now()
                        if self._ai_outreach_daily_limit_reached(chat.who, now=detection_now):
                            self._clear_ai_detection_target(chat.who)
                        else:
                            detection_record = self._record_private_reply_friend_message_for_ai_outreach(chat.who, now=detection_now)
                            detection_state = {chat.who: detection_record} if detection_record else {}
                            if self._should_run_ai_outreach_detection_for_private_reply(chat.who, now=detection_now, state=detection_state):
                                queue_result = self._queue_ai_material_outreach_for_private_reply(chat, message, history)
                                if queue_result.get("evaluation_attempted"):
                                    self._clear_ai_detection_target_if_snapshot(chat.who, detection_record)
                _effective_prompt = self._build_prompt_with_context(
                    chat.who,
                    base_prompt=None,
                    chat_type='private',
                )
                message_content = message_semantic_text
                model_user_message = build_current_turn_user_message(model_message_text)
                fallback_image_path = ""
                quoted_text = ""
                quoted_image_paths = []
                if (
                    self.config.chat_image_recognition_switch
                    and getattr(message, 'type', '') == 'text'
                ):
                    fallback_image_path = self._existing_local_image_path(message_content)
                    if QUOTE_IMAGE_MARKER in message_content:
                        quoted_text, quoted_image_paths = split_quoted_image_message(message_content)
                if self.config.chat_image_recognition_switch and message.type == 'image':
                    self._set_pending_visual_context(
                        chat.who,
                        [message.content],
                        chat_type="private",
                    )
                    reply = self._reply_private_image_message(
                        chat, history, [message.content]
                    )
                    image_reply_context_used = True
                elif self.config.chat_image_recognition_switch and quoted_image_paths:
                    self._set_pending_visual_context(
                        chat.who,
                        quoted_image_paths,
                        chat_type="private",
                    )
                    reply = self._reply_private_image_message(
                        chat, history, quoted_image_paths, quoted_text
                    )
                    image_reply_context_used = True
                elif fallback_image_path:
                    self._set_pending_visual_context(
                        chat.who,
                        [fallback_image_path],
                        chat_type="private",
                    )
                    reply = self._reply_private_image_message(
                        chat, history, [fallback_image_path]
                    )
                    image_reply_context_used = True
                elif self.config.chat_image_recognition_switch:
                    pending_visual_context = self._get_pending_visual_context(
                        chat.who,
                        chat_type="private",
                    )
                    if pending_visual_context:
                        reply = self._reply_private_image_message(
                            chat,
                            history,
                            pending_visual_context.get("image_paths", []),
                            message_content,
                            visual_notes=pending_visual_context.get("visual_notes", []),
                        )
                        image_reply_context_used = True
                    else:
                        reply = self._get_chat_api(chat.who).chat(
                            model_user_message, prompt=_effective_prompt, history=history
                        )
                else:
                    reply = self._get_chat_api(chat.who).chat(
                        model_user_message, prompt=_effective_prompt, history=history
                    )
        except Exception as e:
            if keyword_plan or isinstance(e, sqlite3.DatabaseError):
                raise
            print(traceback.format_exc())
            log(level="ERROR", message=str(e) + f"\n{API_ERROR_REPLY_TEXT}")
            api_error_reply = True
            if self.config.api_error_reply_once and user_key:
                user_data = self._reply_once_user_data(user_key)
                if user_data.get("api_err_notified"):
                    return True
                api_error_should_mark = True
            reply = API_ERROR_REPLY_TEXT

        if is_api_error_reply(reply):
            if self.config.api_error_reply_once and user_key:
                user_data = self._reply_once_user_data(user_key)
                if user_data.get("api_err_notified"):
                    return True
                api_error_should_mark = True
            api_error_reply = True
            parts = self._api_error_reply_parts()
        else:
            preprocess_result = self._preprocess_ai_reply_for_send(
                reply,
                rewrite_func=lambda rewrite_prompt: self._get_chat_api(chat.who).chat(
                    str(reply or ""), prompt=rewrite_prompt, history=[]
                ),
                user_key=user_key,
                scene_label=f"私聊 {chat.who}",
            )
            if preprocess_result["status"] == "skip":
                return True
            if preprocess_result["status"] == "silent":
                return True
            if preprocess_result["status"] == "api_error":
                if self.config.api_error_reply_once and user_key:
                    user_data = self._reply_once_user_data(user_key)
                    if user_data.get("api_err_notified"):
                        return True
                    api_error_should_mark = True
                api_error_reply = True
                parts = self._api_error_reply_parts()
            else:
                reply = preprocess_result["reply"]
                preprocess_fallback_should_mark = bool(preprocess_result.get("mark_fallback"))
                if self.config.chat_split_reply_switch:
                    parts, split_source, split_source_count = prepare_reply_parts_with_source(
                        reply,
                        split_enabled=True,
                        max_count=getattr(self.config, 'chat_split_max_count', 5),
                        clean_enabled=False,
                        fallback_reply="",
                        max_chars=getattr(self.config, 'chat_split_max_chars', 20),
                        allow_chinese_space_split=True,
                        on_clean_empty=self._log_empty_cleaned_reply,
                    )
                    self._log_reply_split_outcome(
                        scene_label="私聊",
                        chat_name=chat.who,
                        split_source=split_source,
                        split_count=split_source_count,
                    )
                else:
                    parts = prepare_reply_parts(
                        reply,
                        split_enabled=False,
                        max_count=getattr(self.config, 'chat_split_max_count', 5),
                        clean_enabled=False,
                        fallback_reply="",
                        max_chars=getattr(self.config, 'chat_split_max_chars', 20),
                        on_clean_empty=self._log_empty_cleaned_reply,
                    )

        if not api_error_reply and not self._private_reply_can_continue(chat):
            return True

        if voice_candidate and not api_error_reply and not preprocess_fallback_should_mark:
            clean_reply = clean_ai_reply_text(reply)
            if classify_voice_reply_text(clean_reply) == "normal":
                context_text = self._private_voice_context_text(message)
                try:
                    voice_sent = self._try_send_voice_reply(
                        chat,
                        clean_reply,
                        state_key=f"private:{chat.who}",
                        limit_count=getattr(self.config, 'chat_voice_reply_limit_count', 5),
                        limit_hours=getattr(self.config, 'chat_voice_reply_limit_hours', 5),
                        context_text=context_text,
                        section_id=str(uuid.uuid4()),
                        expected_sequence=reply_message_sequence,
                        message=message,
                    )
                except _ReplyTurnAborted:
                    return True
                if voice_sent:
                    if image_reply_context_used:
                        self._clear_pending_visual_context(chat.who, chat_type="private")
                    self._save_voice_reply_state()
                    if getattr(self.config, "chat_text_reply_limit_switch", False) and user_key:
                        self.reply_count_store.increment_ai_count(
                            user_key,
                            limit_hours=getattr(self.config, "chat_text_reply_limit_hours", 5),
                        )
                    self._record_reply_metric_success(chat.who, chat_type="private")
                    self._log_reply_contents(
                        "私聊",
                        chat.who,
                        [self._format_reply_log_item(clean_reply, kind="voice")],
                    )
                    return True

        sent_reply_items = []
        send_success, result = self._send_private_ai_reply_parts(
            chat,
            parts,
            message=message,
            expected_sequence=None if api_error_reply else reply_message_sequence,
            sent_items=sent_reply_items,
        )
        if send_success:
            self._log_reply_contents("私聊", chat.who, sent_reply_items)

        if image_reply_context_used and send_success and not api_error_reply:
            if self._pending_visual_context_ready_to_clear(chat.who, chat_type="private"):
                self._clear_pending_visual_context(chat.who, chat_type="private")
            else:
                log(message=f"私聊 {chat.who}：图片摘要尚未回写，暂保留最近图片上下文")

        if send_success and api_error_should_mark:
            self.reply_count_store.mark_api_err_notified(user_key)

        if send_success and preprocess_fallback_should_mark:
            self.reply_count_store.mark_preprocess_fallback_notified(user_key)

        if send_success and getattr(self.config, "chat_text_reply_limit_switch", False) and user_key and not api_error_reply:
            self.reply_count_store.increment_ai_count(
                user_key,
                limit_hours=getattr(self.config, "chat_text_reply_limit_hours", 5),
            )

        if send_success:
            self._record_reply_metric_success(chat.who, chat_type="private")
        return result

    # ----------------------------------------------------------
    # 消息分发与处理
    # ----------------------------------------------------------

    def _maybe_update_chat_memory(self, chat, message):
        """低频增量维护会话记忆。"""
        if not getattr(self.config, 'memory_switch', True) or not self.memory_manager:
            return None
        if getattr(message, 'attr', '') != 'friend':
            return None
        chat_type = str(getattr(chat, 'chat_type', '') or '').strip().lower()
        if chat_type != 'private':
            return None
        system = getattr(self, 'prompt_system', None)
        if system is None:
            system = self._init_prompt_system()
        if not system.auto_memory_enabled_for(chat.who, chat_type='private'):
            return None
        try:
            messages = self.memory_manager.get_messages(
                str(getattr(chat, "who", "") or "").strip(),
                getattr(self.config, 'memory_max_count', 5000),
                chat_type='private',
            )
            api = self._get_other_api(self._get_chat_api_index(chat.who))
            updated = system.update_memory(
                str(getattr(chat, "who", "") or "").strip(),
                messages,
                api,
                chat_type='private',
                protected_count=getattr(
                    self.config,
                    'memory_context_count',
                    50,
                ),
            )
            if updated:
                log(level="INFO", message=f"会话记忆已更新：{chat.who}")
            return updated
        except Exception as e:
            log(level="WARNING", message=f"会话记忆自动维护失败：{e}")
            return None

    def _ensure_chat_memory_background_state(self):
        if not hasattr(self, '_chat_memory_dirty_lock'):
            self._chat_memory_dirty_lock = threading.Lock()
        if not hasattr(self, '_chat_memory_dirty_chats'):
            self._chat_memory_dirty_chats = {}
        if not hasattr(self, '_chat_memory_worker_running'):
            self._chat_memory_worker_running = False

    def _mark_chat_memory_dirty(self, chat, message):
        """记录某个私聊会话需要后台尝试维护记忆。"""
        if not getattr(self.config, 'memory_switch', True) or not self.memory_manager:
            return False
        if getattr(message, 'attr', '') != 'friend':
            return False
        chat_name = str(getattr(chat, 'who', '') or '').strip()
        if not chat_name:
            return False
        chat_type = str(getattr(chat, 'chat_type', '') or '').strip().lower()
        if chat_type != 'private':
            return False
        self._ensure_chat_memory_background_state()
        with self._chat_memory_dirty_lock:
            self._chat_memory_dirty_chats[chat_name] = time.time()
            if self._chat_memory_worker_running:
                return True
            self._chat_memory_worker_running = True
        worker = threading.Thread(target=self._chat_memory_background_worker)
        worker.daemon = True
        worker.start()
        return True

    def _enqueue_existing_chat_memory_checks(self):
        """启动后补偿检查已有聊天记录，避免关闭前未扫到的记忆维护漏跑。"""
        if not getattr(self.config, 'memory_switch', True) or not self.memory_manager:
            return 0
        list_chat_names = getattr(self.memory_manager, "list_chat_names", None)
        if not callable(list_chat_names):
            return 0
        try:
            chat_names = list_chat_names(chat_type='private')
        except Exception as exc:
            log(level="WARNING", message=f"会话记忆启动补偿扫描失败：{exc}")
            return 0
        count = 0
        for chat_name in chat_names:
            chat_name = str(chat_name or "").strip()
            if not chat_name:
                continue
            message = SimpleNamespace(attr='friend')
            chat = SimpleNamespace(who=chat_name, chat_type='private')
            if self._mark_chat_memory_dirty(chat, message):
                count += 1
        return count

    def _pop_chat_memory_dirty_chat(self):
        self._ensure_chat_memory_background_state()
        with self._chat_memory_dirty_lock:
            if not self._chat_memory_dirty_chats:
                self._chat_memory_worker_running = False
                return None
            chat_name = max(
                self._chat_memory_dirty_chats,
                key=self._chat_memory_dirty_chats.get,
            )
            self._chat_memory_dirty_chats.pop(chat_name, None)
            return chat_name

    def _chat_memory_background_worker(self):
        while getattr(self, 'run_flag', True):
            time.sleep(CHAT_MEMORY_BACKGROUND_INTERVAL_SECONDS)
            if not getattr(self, 'run_flag', True):
                break
            chat_name = self._pop_chat_memory_dirty_chat()
            if not chat_name:
                return
            chat = SimpleNamespace(who=chat_name, chat_type='private')
            message = SimpleNamespace(attr='friend')
            self._maybe_update_chat_memory(chat, message)
        self._ensure_chat_memory_background_state()
        with self._chat_memory_dirty_lock:
            self._chat_memory_dirty_chats.clear()
            self._chat_memory_worker_running = False

    def _clear_chat_memory_background_state(self):
        self._ensure_chat_memory_background_state()
        with self._chat_memory_dirty_lock:
            self._chat_memory_dirty_chats.clear()
            self._chat_memory_worker_running = False

    def process_message(self, chat, message):
        """
        处理单条消息的核心分发逻辑：
        1. 黑/白名单过滤
        2. 群聊消息（含 @ 检测和关键词回复）
        3. 普通好友 AI 回复

        :param chat:    聊天窗口子对象
        :param message: 消息对象
        :return:        发送结果
        """
        result = True  # 默认返回成功（WxResponse 类型）

        route = message_routing.route_process_message(self, chat, message)
        action = route.get("action", "skip")
        if action == "skip":
            if getattr(message, "_wxbot_recovery_route_source", ""):
                self._cancel_unfinished_reply_job(message, "reply route is no longer available")
            elif self._is_unresolved_pending_voice_message(message):
                return True
            else:
                self._mark_inbound_no_reply(message)
            return True
        group_user_key = ""
        if action in {"group_keyword_reply", "group_ai"}:
            group_user_key = self._get_group_reply_once_key(chat, message)
            limit_reached = self._text_reply_limit_reached(
                group_user_key,
                chat_type="group",
            )
            route_source = (
                "group_limit"
                if limit_reached
                else "group_keyword"
                if action == "group_keyword_reply"
                else "group_ai"
            )
            if not self._recovery_route_matches(message, route_source):
                self._cancel_unfinished_reply_job(message, "reply route changed during restart")
                return True
            message._wxbot_reply_route_source = route_source
            if not self._reply_job_can_generate(
                chat,
                message,
                chat_type="group",
                route_source=route_source,
            ):
                return True
            if limit_reached:
                limit_handled, limit_result = self._check_text_reply_limit_runtime(
                    chat,
                    group_user_key,
                    message=message,
                    chat_type="group",
                )
                if not limit_handled:
                    self._cancel_unfinished_reply_job(message, "reply limit changed during routing")
                    return True
                self._cancel_unfinished_reply_job(message, "reply limit handled without delivery")
                return limit_result
        if action == "group_keyword_reply":
            log(level="DEBUG", message=f"群组 {chat.who}：命中关键词回复，内容：{message.content}")
            reply_actions = route.get("reply_actions", [])
            send_success, result = self._send_keyword_reply_actions(
                chat,
                reply_actions,
                message=message,
            )
            if send_success:
                if getattr(self.config, "group_text_reply_limit_switch", False) and group_user_key:
                    self.reply_count_store.increment_ai_count(
                        group_user_key,
                        limit_hours=getattr(self.config, "group_text_reply_limit_hours", 5),
                    )
                self._record_reply_metric_success(chat.who, chat_type="group")
                self._record_keyword_reply_success(chat.who, chat_type="group", action_count=len(reply_actions))
            time.sleep(1)
            self._cancel_unfinished_reply_job(message, "keyword routing completed without delivery")
            return result
        if action == "group_ai":
            content_without_at = strip_group_mention(
                message.content,
                getattr(self.config, "AtMe", ""),
            )
            log(level="DEBUG", message=f"群组 {chat.who}：触发 AI 回复，内容：{content_without_at}")
            content_with_sender = f"{message.sender}: {format_model_message_text({'type': getattr(message, 'type', ''), 'content': content_without_at})}"
            model_group_user_message = build_current_turn_user_message(content_with_sender)
            group_voice_candidate_hit = False
            group_preprocess_fallback_should_mark = False
            try:
                history = []
                if self.config.memory_switch and self.memory_manager:
                    repaired_history = self._repair_context_before_ai(
                        chat,
                        message,
                        chat_type="group",
                    )
                    if self.config.memory_context_switch:
                        history = self._get_model_context_history(
                            chat.who,
                            event_ids=self._reply_event_ids(message),
                            chat_type="group",
                            extra_messages=repaired_history,
                        )
                # 构建有效 prompt；拆分改为发送前本地处理，不再注入模型格式要求
                group_voice_candidate_hit = group_voice_candidate(self.config, message)
                _effective_group_prompt = self._build_prompt_with_context(
                    chat.who,
                    chat_type='group',
                )
                if self.config.group_image_recognition_switch and message.type == 'image':
                    reply = self._reply_group_image_message(
                        chat,
                        message,
                        history,
                        [message.content],
                    )
                elif self.config.group_image_recognition_switch and QUOTE_IMAGE_MARKER in content_without_at:
                    text_part, image_paths = split_quoted_image_message(content_without_at)
                    reply = self._reply_group_image_message(
                        chat,
                        message,
                        history,
                        image_paths,
                        text_part.strip(),
                    )
                elif (
                    self.config.group_image_recognition_switch
                    and self._text_references_pending_visual_context(content_without_at)
                ):
                    pending_visual_context = self._get_pending_visual_context(
                        chat.who,
                        chat_type="group",
                    )
                    if pending_visual_context:
                        reply = self._reply_group_image_message(
                            chat,
                            message,
                            history,
                            pending_visual_context.get("image_paths", []),
                            content_without_at,
                            image_senders=pending_visual_context.get("image_senders", []),
                        )
                    else:
                        group_api = self._get_group_api(chat.who)
                        reply = group_api.chat(model_group_user_message, prompt=_effective_group_prompt, history=history)
                else:
                    group_api = self._get_group_api(chat.who)
                    reply = group_api.chat(model_group_user_message, prompt=_effective_group_prompt, history=history)
            except Exception as e:
                if isinstance(e, sqlite3.DatabaseError):
                    raise
                print(traceback.format_exc())
                log(level="ERROR", message=str(e) + "\n群组中调用AI回复错误！！")
                reply = API_ERROR_REPLY_TEXT
            group_api_error_reply = False
            # 接口调用失败时替换为配置的固定回复；留空则静默
            if is_api_error_reply(reply):
                group_api_error_reply = True
                group_user_key = self._get_group_reply_once_key(chat, message)
                group_api_error_should_mark = False
                if getattr(self.config, "api_error_reply_once", False) and group_user_key:
                    user_data = self._reply_once_user_data(group_user_key)
                    if user_data.get("api_err_notified"):
                        self._cancel_unfinished_reply_job(message, "API error notice already sent")
                        return True
                    group_api_error_should_mark = True
                parts = self._api_error_reply_parts()
            else:
                group_api_error_should_mark = False
                group_user_key = self._get_group_reply_once_key(chat, message)
                preprocess_result = self._preprocess_ai_reply_for_send(
                    reply,
                    rewrite_func=lambda rewrite_prompt: self._get_group_api(chat.who).chat(
                        str(reply or ""), prompt=rewrite_prompt, history=[]
                    ),
                    user_key=group_user_key,
                    scene_label=f"群聊 {chat.who}",
                )
                if preprocess_result["status"] == "skip":
                    self._cancel_unfinished_reply_job(message, "reply preprocessing skipped delivery")
                    return True
                if preprocess_result["status"] == "silent":
                    self._cancel_unfinished_reply_job(message, "reply preprocessing requested silence")
                    return True
                if preprocess_result["status"] == "api_error":
                    group_api_error_reply = True
                    if getattr(self.config, "api_error_reply_once", False) and group_user_key:
                        user_data = self._reply_once_user_data(group_user_key)
                        if user_data.get("api_err_notified"):
                            self._cancel_unfinished_reply_job(message, "API error notice already sent")
                            return True
                        group_api_error_should_mark = True
                    parts = self._api_error_reply_parts()
                else:
                    reply = preprocess_result["reply"]
                    group_preprocess_fallback_should_mark = bool(preprocess_result.get("mark_fallback"))
                    if self.config.group_split_reply_switch:
                        parts, split_source, split_source_count = prepare_reply_parts_with_source(
                            reply,
                            split_enabled=True,
                            max_count=getattr(self.config, 'group_split_max_count', 5),
                            clean_enabled=False,
                            fallback_reply="",
                            max_chars=getattr(self.config, 'group_split_max_chars', 20),
                            on_clean_empty=self._log_empty_cleaned_reply,
                        )
                        self._log_reply_split_outcome(
                            scene_label="群聊",
                            chat_name=chat.who,
                            split_source=split_source,
                            split_count=split_source_count,
                        )
                    else:
                        parts = prepare_reply_parts(
                            reply,
                            split_enabled=False,
                            max_count=getattr(self.config, 'group_split_max_count', 5),
                            clean_enabled=False,
                            fallback_reply="",
                            max_chars=getattr(self.config, 'group_split_max_chars', 20),
                            on_clean_empty=self._log_empty_cleaned_reply,
                        )

            if group_voice_candidate_hit and not group_api_error_reply and not group_preprocess_fallback_should_mark:
                clean_reply = clean_ai_reply_text(reply)
                if classify_voice_reply_text(clean_reply) == "normal":
                    group_context_text = self._group_voice_context_text(message, content_without_at)
                    try:
                        voice_sent = self._try_send_voice_reply(
                            chat,
                            clean_reply,
                            state_key=f"group:{chat.who}",
                            limit_count=getattr(self.config, 'group_voice_reply_limit_count', 5),
                            limit_hours=getattr(self.config, 'group_voice_reply_limit_hours', 5),
                            context_text=group_context_text,
                            message=message,
                        )
                    except _ReplyTurnAborted:
                        return True
                    if voice_sent:
                        self._save_voice_reply_state()
                        if getattr(self.config, "group_text_reply_limit_switch", False) and group_user_key:
                            self.reply_count_store.increment_ai_count(
                                group_user_key,
                                limit_hours=getattr(self.config, "group_text_reply_limit_hours", 5),
                            )
                        self._record_reply_metric_success(chat.who, chat_type="group")
                        self._log_reply_contents(
                            "群聊",
                            chat.who,
                            [self._format_reply_log_item(clean_reply, kind="voice")],
                        )
                        return True

            _at_msg = self.config.group_reply_at_msg
            _quote = self.config.group_reply_quote
            sent_reply_items = []
            source = ReplySource.ERROR if group_api_error_reply else ReplySource.AI
            reply_actions = list(self._reply_actions_from_text(parts, source=source))
            if group_api_error_reply and not reply_actions:
                self._cancel_unfinished_reply_job(message, "API error notice is configured as silent")
                return True
            if _quote and reply_actions:
                first = reply_actions[0]
                reply_actions[0] = ReplyAction(ReplyKind.QUOTE, first.content, first.source)
            delivery = self._deliver_reply_actions(
                chat,
                message,
                reply_actions,
                chat_type="group",
                at_first=message.sender if _at_msg else "",
                sent_items=sent_reply_items,
            )
            sent_any = bool(delivery and delivery.completed)
            result = self._delivery_result_is_handled(delivery)

            if sent_any:
                if group_api_error_should_mark:
                    group_user_key = self._get_group_reply_once_key(chat, message)
                    if group_user_key:
                        self.reply_count_store.mark_api_err_notified(group_user_key)
                if group_preprocess_fallback_should_mark:
                    group_user_key = self._get_group_reply_once_key(chat, message)
                    if group_user_key:
                        self.reply_count_store.mark_preprocess_fallback_notified(group_user_key)
                if (
                    not group_api_error_reply
                    and getattr(self.config, "group_text_reply_limit_switch", False)
                    and group_user_key
                ):
                    self.reply_count_store.increment_ai_count(
                        group_user_key,
                        limit_hours=getattr(self.config, "group_text_reply_limit_hours", 5),
                    )
                self._record_reply_metric_success(chat.who, chat_type="group")
                self._log_reply_contents("群聊", chat.who, sent_reply_items)
            self._cancel_unfinished_reply_job(message, "group routing completed without delivery")
            return result

        if action == "private_ai":
            result = self._enqueue_private_message_for_ai(chat, message)
            return result

        return result

    def _ensure_chat_api_failover_state(self):
        if not hasattr(self, '_chat_api_failover_lock'):
            self._chat_api_failover_lock = threading.RLock()
        if not hasattr(self, 'active_chat_api_index'):
            self.active_chat_api_index = int(getattr(self.config, 'api_index', 0) or 0)
        if not hasattr(self, 'chat_api_fail_count'):
            self.chat_api_fail_count = 0
        if not hasattr(self, 'chat_api_using_backup'):
            self.chat_api_using_backup = False
        if not hasattr(self, 'next_primary_chat_api_probe_at'):
            self.next_primary_chat_api_probe_at = None

    def _get_chat_api_failover_now(self):
        return time.time()

    def _sync_runtime_api_config_fields_from_config(self):
        config_data = getattr(self.config, 'config', {}) or {}
        api_configs = config_data.get('api_configs', getattr(self.config, 'api_configs', []))
        if not isinstance(api_configs, list):
            api_configs = []
        self.config.api_configs = api_configs

        try:
            api_index = int(config_data.get('api_index', getattr(self.config, 'api_index', 0)))
        except (TypeError, ValueError):
            api_index = 0
        if api_configs:
            api_index = max(0, min(len(api_configs) - 1, api_index))
        else:
            api_index = 0
        self.config.api_index = api_index
        self.config.config['api_index'] = api_index

        api_capability_map = config_data.get('api_capability_map', getattr(self.config, 'api_capability_map', {}))
        if not isinstance(api_capability_map, dict):
            api_capability_map = {}
        self.config.api_capability_map = api_capability_map
        self.config.config['api_capability_map'] = api_capability_map

        try:
            backup_index = int(
                config_data.get('backup_chat_api_index', getattr(self.config, 'backup_chat_api_index', -1))
            )
        except (TypeError, ValueError):
            backup_index = -1
        if (
            len(api_configs) < 2
            or backup_index < 0
            or backup_index >= len(api_configs)
            or backup_index == api_index
        ):
            backup_index = -1
        self.config.backup_chat_api_index = backup_index
        self.config.config['backup_chat_api_index'] = backup_index

        self.config.backup_chat_api_failover_threshold = coerce_int_range(
            config_data.get(
                'backup_chat_api_failover_threshold',
                getattr(self.config, 'backup_chat_api_failover_threshold', 3),
            ),
            3,
            1,
            10,
        )
        self.config.config['backup_chat_api_failover_threshold'] = self.config.backup_chat_api_failover_threshold

        current = api_configs[api_index] if api_configs else {}
        self.config.prompt = ''
        self.config.current_api_config = build_api_config_snapshot(
            current,
            prompt=self.config.prompt,
            max_retries=getattr(self.config, 'max_retries', 5),
            interface_index=api_index,
        )

    def _reset_chat_api_failover_state(self, *, active_index=None):
        self._ensure_chat_api_failover_state()
        with self._chat_api_failover_lock:
            self.chat_api_fail_count = 0
            self.chat_api_using_backup = False
            self.next_primary_chat_api_probe_at = None
            if active_index is None:
                active_index = self._get_primary_chat_api_index()
            self.active_chat_api_index = active_index

    def _get_primary_chat_api_index(self):
        try:
            index = int(getattr(self.config, 'api_index', 0))
        except (TypeError, ValueError):
            index = 0
        return max(0, index)

    def _get_backup_chat_api_index(self):
        try:
            index = int(getattr(self.config, 'backup_chat_api_index', -1))
        except (TypeError, ValueError):
            index = -1
        api_configs = getattr(self.config, 'api_configs', []) or []
        if (
            not isinstance(api_configs, list)
            or len(api_configs) < 2
            or index < 0
            or index >= len(api_configs)
            or index == self._get_primary_chat_api_index()
        ):
            return -1
        return index

    def _get_backup_chat_api_failover_threshold(self):
        try:
            threshold = int(getattr(self.config, 'backup_chat_api_failover_threshold', 3))
        except (TypeError, ValueError):
            threshold = 3
        return max(1, min(10, threshold))

    def _get_active_default_chat_api_index(self):
        self._ensure_chat_api_failover_state()
        with self._chat_api_failover_lock:
            if self.chat_api_using_backup:
                backup_index = self._get_backup_chat_api_index()
                if backup_index >= 0:
                    self.active_chat_api_index = backup_index
                    return backup_index
                self.chat_api_using_backup = False
            primary_index = self._get_primary_chat_api_index()
            if not self.chat_api_using_backup:
                self.active_chat_api_index = primary_index
            return self.active_chat_api_index

    def _get_chat_api_name(self, index):
        try:
            fallback_index = int(index)
        except (TypeError, ValueError):
            fallback_index = 0
        return format_api_display_name(
            getattr(self.config, 'api_configs', []) or [],
            index,
            fallback=f"接口 {fallback_index + 1}",
        )

    def _get_current_chat_api_display_name(self):
        api_configs = getattr(self.config, "api_configs", []) or []
        if not isinstance(api_configs, list) or not api_configs:
            return "未连接"
        try:
            index = self._get_active_default_chat_api_index()
        except Exception:
            try:
                index = int(getattr(self, "active_chat_api_index", getattr(self.config, "api_index", 0)) or 0)
            except (TypeError, ValueError):
                index = 0
        return format_api_display_name(api_configs, index, fallback="未连接")

    def _record_primary_chat_api_success(self):
        self._ensure_chat_api_failover_state()
        with self._chat_api_failover_lock:
            if self.chat_api_using_backup:
                return
            self.chat_api_fail_count = 0
            self.next_primary_chat_api_probe_at = None
            self.active_chat_api_index = self._get_primary_chat_api_index()

    def _record_primary_chat_api_failure(self):
        self._ensure_chat_api_failover_state()
        with self._chat_api_failover_lock:
            if self.chat_api_using_backup:
                return
            self.chat_api_fail_count += 1
            primary_index = self._get_primary_chat_api_index()
            threshold = self._get_backup_chat_api_failover_threshold()
            log(message=f"主聊天接口调用失败 {self.chat_api_fail_count}/{threshold}，接口：{self._get_chat_api_name(primary_index)}")
            if self.chat_api_fail_count < threshold:
                return
            backup_index = self._get_backup_chat_api_index()
            if backup_index < 0:
                return
            self.chat_api_using_backup = True
            self.active_chat_api_index = backup_index
            self.chat_api_fail_count = 0
            self.next_primary_chat_api_probe_at = (
                self._get_chat_api_failover_now() + PRIMARY_CHAT_API_RECOVERY_CHECK_INTERVAL_SECONDS
            )
            log(message=f"主聊天接口连续失败达到阈值，已切换到备用聊天接口：{self._get_chat_api_name(backup_index)}")

    def _record_api_request_by_type(self, request_type):
        if str(request_type or "").strip() == "other":
            self._record_other_api_request()
        else:
            self._record_chat_api_request()

    def _retry_current_message_with_backup(self, *args, request_type="chat", **kwargs):
        self._ensure_chat_api_failover_state()
        backup_index = self._get_backup_chat_api_index()
        if backup_index < 0:
            return False, None
        with self._chat_api_failover_lock:
            should_retry = self.chat_api_using_backup and self.active_chat_api_index == backup_index
        if not should_retry:
            return False, None
        log(message=f"主聊天接口失败，当前消息改用备用接口：{self._get_chat_api_name(backup_index)}")
        backup_api = self._get_api_instance_by_index(backup_index)
        try:
            self._record_api_request_by_type(request_type)
            result = backup_api.chat(*args, **kwargs)
        except Exception as exc:
            log(level="WARNING", message=f"备用聊天接口调用失败：{exc}")
            log(level="WARNING", message="主备接口都失败，本次未发送回复")
            return True, API_ERROR_REPLY_TEXT
        if is_api_error_reply(result):
            log(level="WARNING", message="主备接口都失败，本次未发送回复")
        return True, result

    def _try_restore_primary_chat_api(self, *args, request_type="chat", **kwargs):
        self._ensure_chat_api_failover_state()
        primary_index = self._get_primary_chat_api_index()
        backup_index = self._get_backup_chat_api_index()
        if backup_index < 0:
            return False, None

        with self._chat_api_failover_lock:
            if not self.chat_api_using_backup or self.active_chat_api_index != backup_index:
                return False, None
            now = self._get_chat_api_failover_now()
            probe_at = self.next_primary_chat_api_probe_at
            if probe_at is None or now < probe_at:
                return False, None
            self.next_primary_chat_api_probe_at = now + PRIMARY_CHAT_API_RECOVERY_CHECK_INTERVAL_SECONDS

        log(message=f"已到主聊天接口恢复检测时间，开始探测：{self._get_chat_api_name(primary_index)}")
        try:
            self._record_api_request_by_type(request_type)
            result = self.api.chat(*args, **kwargs)
        except Exception as exc:
            log(message=f"主聊天接口恢复探测失败，继续使用备用聊天接口：{exc}")
            return False, None

        if is_api_error_reply(result):
            log(message="主聊天接口恢复探测失败，继续使用备用聊天接口：主接口仍返回错误")
            return False, None

        with self._chat_api_failover_lock:
            self.chat_api_using_backup = False
            self.chat_api_fail_count = 0
            self.active_chat_api_index = primary_index
            self.next_primary_chat_api_probe_at = None
        log(message=f"主聊天接口恢复探测成功，已切回主聊天接口：{self._get_chat_api_name(primary_index)}")
        return True, result

    def _get_api_instance_by_index(self, index):
        primary_index = self._get_primary_chat_api_index()
        if index == primary_index:
            return self.api
        if index not in self.api_cache:
            self.api_cache[index] = self._init_api_by_index(index)
        return self.api_cache[index]

    def _wrap_chat_api_for_failover(self, api, *, index, tracked_default, request_type="chat"):
        if not tracked_default:
            return self._wrap_api_request_counter(api, request_type)

        def chat_callable(*args, **kwargs):
            if self.chat_api_using_backup and index == self._get_backup_chat_api_index():
                restored, result = self._try_restore_primary_chat_api(*args, request_type=request_type, **kwargs)
                if restored:
                    return result
                log(message="当前使用备用聊天接口回复")
            try:
                self._record_api_request_by_type(request_type)
                result = api.chat(*args, **kwargs)
            except Exception:
                if not self.chat_api_using_backup and index == self._get_primary_chat_api_index():
                    self._record_primary_chat_api_failure()
                    retried, retry_result = self._retry_current_message_with_backup(*args, request_type=request_type, **kwargs)
                    if retried:
                        return retry_result
                raise
            if is_api_error_reply(result):
                if not self.chat_api_using_backup and index == self._get_primary_chat_api_index():
                    self._record_primary_chat_api_failure()
                    retried, retry_result = self._retry_current_message_with_backup(*args, request_type=request_type, **kwargs)
                    if retried:
                        return retry_result
            elif not self.chat_api_using_backup and index == self._get_primary_chat_api_index():
                self._record_primary_chat_api_success()
            return result

        return _ChatAPIFailoverProxy(api, chat_callable)

    def _resolve_chat_api_selection(self, user_name):
        if self._is_private_whitelist_user(user_name):
            idx = (getattr(self.config, 'chat_api_map', {}) or {}).get(user_name)
            if idx is not None:
                try:
                    idx = int(idx)
                    if idx >= 0:
                        return idx, False
                except (TypeError, ValueError):
                    pass
        return self._get_active_default_chat_api_index(), True

    def _resolve_group_api_selection(self, group_name):
        idx = (getattr(self.config, 'group_api_map', {}) or {}).get(group_name)
        if idx is not None:
            try:
                idx = int(idx)
                if idx >= 0:
                    return idx, False
            except (TypeError, ValueError):
                pass
        return self._get_active_default_chat_api_index(), True

    def _get_chat_api(self, user_name):
        """获取私聊用户对应的 AI 接口实例（白名单模式查 chat_api_map，否则用默认接口/备用接口）。"""
        idx, tracked_default = self._resolve_chat_api_selection(user_name)
        api = self._get_api_instance_by_index(idx)
        return self._wrap_chat_api_for_failover(api, index=idx, tracked_default=tracked_default)

    def _get_chat_api_index(self, user_name):
        """获取本轮私聊最终回复接口索引；白名单专属接口优先，否则跟随主/备聊天接口。"""
        idx, _tracked_default = self._resolve_chat_api_selection(user_name)
        return idx

    def _get_group_api(self, group_name):
        """
        获取群聊对应的 AI 接口实例。
        - 若配置了 group_api_map 映射，则返回对应接口（惰性初始化并缓存）
        - 否则返回当前活动的主/备聊天接口
        """
        idx, tracked_default = self._resolve_group_api_selection(group_name)
        api = self._get_api_instance_by_index(idx)
        return self._wrap_chat_api_for_failover(api, index=idx, tracked_default=tracked_default)

    def _get_other_api(self, index=None):
        try:
            idx = int(index)
        except (TypeError, ValueError):
            idx = self._get_active_default_chat_api_index()
        api = self._get_api_instance_by_index(idx)
        return self._wrap_api_request_counter(api, "other")

    def _get_group_api_index(self, group_name):
        """Return the final reply API index for this group."""
        idx, _tracked_default = self._resolve_group_api_selection(group_name)
        return idx

    def _api_supports_capability(self, index, capability):
        return api_supports_capability(
            getattr(self.config, 'api_capability_map', {}) or {},
            index,
            capability,
        )

    def _chat_reply_api_supports_vision(self, user_name):
        return self._api_supports_capability(self._get_chat_api_index(user_name), "vision")

    def _group_reply_api_supports_vision(self, group_name):
        return self._api_supports_capability(self._get_group_api_index(group_name), "vision")

    def _is_private_whitelist_user(self, user_name):
        listen_list = getattr(self.config, 'listen_list', []) or []
        return isinstance(listen_list, list) and user_name in listen_list

    def _get_chat_prompt(self, user_name):
        """获取私聊用户对应的 prompt 内容；白名单用户优先使用专属绑定。"""
        prompt_map = getattr(self.config, 'chat_prompt_map', {}) or {}
        name = (
            prompt_map.get(user_name)
            if self._is_private_whitelist_user(user_name)
            else ''
        ) or self.config.default_prompt
        return self.config.get_prompt_content(name)

    def _get_group_prompt(self, group_name):
        """获取群组对应的 prompt 内容（查 group_prompt_map，未配置则用 default_prompt）"""
        name = self.config.group_prompt_map.get(group_name) or self.config.default_prompt
        return self.config.get_prompt_content(name)

    # ----------------------------------------------------------
    # 拆分多条回复辅助方法
    # ----------------------------------------------------------

    def _build_image_recognition_message(self, chat_type="private", sender=""):
        return build_image_recognition_message(chat_type, sender=sender)

    def _build_image_parse_block(self, visual_note_text=""):
        return SystemPromptStore().render(
            IMAGE_PARSE_PROMPT_FILE,
            {},
        ).strip()

    def _build_image_user_message(
        self,
        chat_type="private",
        sender="",
        attached_text="",
        image_count=1,
        visual_notes=None,
        image_senders=None,
    ):
        return build_image_user_message(
            chat_type,
            sender=sender,
            attached_text=attached_text,
            image_count=image_count,
            visual_notes=visual_notes,
            image_senders=image_senders,
        )

    def _build_image_description_prompt(self, chat_type="private", sender="", attached_text=""):
        return build_image_description_prompt(chat_type, sender=sender, attached_text=attached_text)

    def _get_vision_bridge(self):
        bridge = getattr(self, "_vision_bridge", None)
        if bridge is None:
            bridge = VisionBridge(
                description_prompt_builder=self._build_image_description_prompt,
                description_system_prompt=IMAGE_DESCRIPTION_SYSTEM_PROMPT,
                log_warning=lambda message: log(level="WARNING", message=message),
            )
            self._vision_bridge = bridge
        return bridge

    def _get_image_reply_pipeline(self):
        pipeline = getattr(self, "_image_reply_pipeline", None)
        if pipeline is None:
            pipeline = ImageReplyPipeline(
                prompt_builder=self._build_prompt_with_context,
                image_parse_block_builder=self._build_image_parse_block,
                user_message_builder=self._build_image_user_message,
                vision_bridge=self._get_vision_bridge(),
                log_info=lambda message: log(message=message),
            )
            self._image_reply_pipeline = pipeline
        return pipeline

    def _reply_image_message(
        self,
        *,
        chat_name,
        chat_type,
        history,
        final_api,
        final_api_supports_vision,
        recognition_api_index,
        image_path="",
        image_paths=None,
        attached_text="",
        sender="",
        image_senders=None,
        visual_notes=None,
    ):
        self._record_image_api_request()
        return self._get_image_reply_pipeline().reply(ImageReplyRequest(
            chat_name=chat_name,
            chat_type=chat_type,
            attached_text=attached_text,
            sender=sender,
            history=history,
            final_api=final_api,
            recognition_api=(
                None if final_api_supports_vision
                else self._wrap_chat_api_for_failover(
                    self._get_api_instance_by_index(recognition_api_index),
                    index=recognition_api_index,
                    tracked_default=recognition_api_index == self._get_primary_chat_api_index(),
                    request_type="chat",
                )
            ),
            final_api_supports_vision=final_api_supports_vision,
            image_path=image_path,
            image_paths=image_paths,
            image_senders=image_senders,
            visual_notes=visual_notes,
            on_visual_notes=lambda paths, notes: self._remember_visual_notes(
                chat_name,
                paths,
                notes,
                chat_type=chat_type,
            ),
            final_api_index=(
                self._get_chat_api_index(chat_name)
                if chat_type == 'private'
                else self._get_group_api_index(chat_name)
            ),
            recognition_api_index=recognition_api_index,
        ))

    def _reply_private_image_message(self, chat, history, image_paths=None, attached_text="", visual_notes=None):
        normalized_paths = (
            [str(path or "").strip() for path in image_paths if str(path or "").strip()]
            if isinstance(image_paths, (list, tuple))
            else [str(image_paths or "").strip()] if str(image_paths or "").strip() else []
        )
        normalized_notes = self._normalize_visual_note_slots(normalized_paths, visual_notes)
        if normalized_paths and not any(normalized_notes):
            try:
                normalized_notes = self._generate_visual_notes_for_image_paths(
                    "private",
                    normalized_paths,
                    sender=getattr(chat, "who", ""),
                    attached_text="",
                )
                self._remember_visual_notes(
                    chat.who,
                    normalized_paths,
                    normalized_notes,
                    chat_type="private",
                )
            except Exception as exc:
                log(level="WARNING", message=f"生成私聊图片摘要失败：{exc}")
                normalized_notes = []
        return self._reply_image_message(
            chat_name=chat.who,
            chat_type='private',
            history=history,
            final_api=self._get_chat_api(chat.who),
            final_api_supports_vision=self._chat_reply_api_supports_vision(chat.who),
            recognition_api_index=self.config.chat_image_recognition_api,
            image_path=normalized_paths[0] if len(normalized_paths) == 1 else "",
            image_paths=normalized_paths,
            attached_text=attached_text,
            visual_notes=normalized_notes,
        )

    def _get_image_recognition_api_for_chat(self, chat_type):
        if str(chat_type or "").strip().lower() == "group":
            api_index = getattr(self.config, "group_image_recognition_api", 0)
        else:
            api_index = getattr(self.config, "chat_image_recognition_api", 0)
        return self._wrap_chat_api_for_failover(
            self._get_api_instance_by_index(api_index),
            index=api_index,
            tracked_default=api_index == self._get_primary_chat_api_index(),
            request_type="chat",
        )

    def _generate_visual_notes_for_image_paths(
        self,
        chat_type,
        image_paths,
        *,
        sender="",
        senders=None,
        attached_text="",
    ):
        normalized_paths = [
            str(path or "").strip()
            for path in (image_paths or [])
            if str(path or "").strip()
        ]
        if not normalized_paths:
            return []
        notes = []
        recognition_api = self._get_image_recognition_api_for_chat(chat_type)
        normalized_senders = self._normalize_visual_sender_slots(normalized_paths, senders)
        for index, image_path in enumerate(normalized_paths):
            note = self._get_vision_bridge().analyze(
                image_path=image_path,
                recognition_api=recognition_api,
                chat_type=chat_type,
                sender=normalized_senders[index] or sender,
                attached_text=attached_text,
            )
            notes.append(note.render())
        return notes

    def _image_recognition_enabled_for_chat(self, chat_type):
        chat_type = str(chat_type or "").strip().lower()
        if chat_type == "group":
            return bool(getattr(self.config, "group_image_recognition_switch", False))
        return bool(getattr(self.config, "chat_image_recognition_switch", False))

    def _extract_message_image_paths(self, message):
        msg_type = str(getattr(message, "type", "") or "").strip().lower()
        content = str(getattr(message, "content", "") or "").strip()
        if msg_type == "image":
            return [content] if content else []
        if not content or QUOTE_IMAGE_MARKER not in content:
            return []
        _text_part, image_paths = split_quoted_image_message(content)
        return [path for path in image_paths if str(path or "").strip()]

    def _reply_group_image_message(
        self,
        chat,
        message,
        history,
        image_paths=None,
        attached_text="",
        image_senders=None,
    ):
        normalized_paths = (
            [str(path or "").strip() for path in image_paths if str(path or "").strip()]
            if isinstance(image_paths, (list, tuple))
            else [str(image_paths or "").strip()] if str(image_paths or "").strip() else []
        )
        normalized_senders = self._normalize_visual_sender_slots(
            normalized_paths,
            image_senders,
            fallback=getattr(message, "sender", ""),
        )
        normalized_notes = self._normalize_visual_note_slots(normalized_paths, None)
        if normalized_paths:
            try:
                normalized_notes = self._generate_visual_notes_for_image_paths(
                    "group",
                    normalized_paths,
                    senders=normalized_senders,
                    attached_text="",
                )
                self._remember_visual_notes(
                    chat.who,
                    normalized_paths,
                    normalized_notes,
                    chat_type="group",
                )
            except Exception as exc:
                log(level="WARNING", message=f"生成群聊图片摘要失败：{exc}")
                normalized_notes = []
        return self._reply_image_message(
            chat_name=chat.who,
            chat_type='group',
            history=history,
            final_api=self._get_group_api(chat.who),
            final_api_supports_vision=self._group_reply_api_supports_vision(chat.who),
            recognition_api_index=self.config.group_image_recognition_api,
            image_path=normalized_paths[0] if len(normalized_paths) == 1 else "",
            image_paths=normalized_paths,
            attached_text=attached_text,
            sender=getattr(message, 'sender', ''),
            image_senders=normalized_senders,
            visual_notes=normalized_notes,
        )

    def _ensure_message_runtime_state(self):
        if not hasattr(self, '_chat_merge_lock'):
            self._chat_merge_lock = threading.Lock()
        if not hasattr(self, '_private_message_pipelines'):
            self._private_message_pipelines = {}
        if not hasattr(self, '_group_message_pipelines'):
            self._group_message_pipelines = {}
        if not hasattr(self, '_pending_private_voice_transcription'):
            self._pending_private_voice_transcription = {}
        if not hasattr(self, '_pending_private_voice_sequence'):
            self._pending_private_voice_sequence = {}
        if not hasattr(self, '_pending_visual_contexts'):
            self._pending_visual_contexts = {}
        if not hasattr(self, '_memory_context_repair_state'):
            self._memory_context_repair_state = {}
        if not hasattr(self, '_memory_context_repair_lock'):
            self._memory_context_repair_lock = threading.Lock()

    def _get_private_message_sequence(self, chat_name, chat_type="private"):
        self._ensure_message_runtime_state()
        name = str(chat_name or "").strip()
        if not name:
            return 0
        return self._message_store.conversation_version(name, chat_type=chat_type)

    def _should_skip_private_ai_message(self, message):
        if getattr(message, '_skip_ai_reply', False):
            return True
        msg_type = getattr(message, 'type', '')
        if msg_type == 'voice' and not getattr(self.config, 'chat_voice_recognition_switch', False):
            return True
        if msg_type == 'image' and not getattr(self.config, 'chat_image_recognition_switch', False):
            return True
        return False

    def _reply_once_user_data(self, user_key):
        scope = "group" if str(user_key or "").startswith("group:") else "chat"
        limit_hours = getattr(self.config, f"{scope}_text_reply_limit_hours", 5)
        return self.reply_count_store.get_user(user_key, now=datetime.now(), limit_hours=limit_hours)

    def _ensure_pending_private_voice_transcription_state(self):
        if not hasattr(self, "_pending_private_voice_transcription") or self._pending_private_voice_transcription is None:
            self._pending_private_voice_transcription = {}
        if not hasattr(self, "_pending_private_voice_sequence") or self._pending_private_voice_sequence is None:
            self._pending_private_voice_sequence = {}

    @staticmethod
    def _pending_voice_task_key(chat):
        chat_ref = ConversationRef.from_wx_chat(chat)
        if chat_ref.chat_type == "group":
            return ("group", chat_ref.who)
        return chat_ref.who

    @staticmethod
    def _pending_voice_item_expired(item):
        attempts = int(item.get("reread_attempts") or 0)
        return attempts >= VOICE_TRANSCRIPTION_MAX_REREAD_ATTEMPTS

    @staticmethod
    def _pending_voice_item_deadline_reached(item):
        first_seen_at = float(item.get("first_seen_at") or time.time())
        max_wait = VOICE_TRANSCRIPTION_RETRY_DELAY_SECONDS * VOICE_TRANSCRIPTION_MAX_REREAD_ATTEMPTS
        return time.time() - first_seen_at >= max_wait

    def _terminalize_exhausted_voice_recovery(self, items):
        store = getattr(self, "_message_store", None)
        if store is None:
            return 0
        event_ids = [
            str(item.get("event_id") or "").strip()
            for item in (items or [])
            if str(item.get("event_id") or "").strip()
        ]
        if not event_ids:
            return 0
        store.mark_inbound_events(event_ids, "handled")
        return len(event_ids)

    @staticmethod
    def _is_unresolved_pending_voice_message(message):
        return bool(
            getattr(message, "_wxbot_pending_voice_key", "")
            and not getattr(message, "_wxbot_pending_voice_resolved", False)
        )

    def _private_pipeline_has_unresolved_voice_locked(self, pipeline):
        if not isinstance(pipeline, dict):
            return False
        for message in pipeline.get("open_messages") or []:
            if self._is_unresolved_pending_voice_message(message):
                return True
        return False

    def _insert_pending_private_voice_placeholder_locked(self, name, chat, message, key):
        pipeline = self._private_message_pipeline(name)
        if not pipeline:
            return False
        for existing in pipeline.get("open_messages") or []:
            if getattr(existing, "_wxbot_pending_voice_key", "") == key:
                return False
        try:
            setattr(message, "_wxbot_pending_voice_key", key)
            setattr(message, "_wxbot_pending_voice_resolved", False)
        except Exception:
            pass
        batch_kind = self._private_message_batch_kind(message)
        open_kind = str(pipeline.get("open_kind") or "text").strip().lower()
        if not pipeline["open_messages"]:
            pipeline["open_started_at"] = time.time()
            pipeline["open_kind"] = batch_kind
        else:
            pipeline["open_kind"] = open_kind if open_kind == batch_kind else "mixed"
        pipeline["open_messages"].append(message)
        base_delay = self._private_message_merge_delay()
        self._schedule_private_message_pipeline_locked(
            chat,
            pipeline,
            self._private_message_effective_merge_delay(pipeline, base_delay),
            max_wait_base_delay=base_delay,
        )
        return True

    def _replace_pending_private_voice_placeholder_locked(self, name, key, resolved):
        pipeline = self._private_message_pipelines.get(name)
        if not isinstance(pipeline, dict):
            return False
        for index, message in enumerate(pipeline.get("open_messages") or []):
            if getattr(message, "_wxbot_pending_voice_key", "") != key:
                continue
            try:
                setattr(resolved, "_wxbot_pending_voice_key", key)
                setattr(resolved, "_wxbot_pending_voice_resolved", True)
                setattr(resolved, "_wxbot_media_prepared", True)
            except Exception:
                pass
            pipeline["open_messages"][index] = resolved
            return True
        return False

    def _drop_pending_private_voice_placeholder_locked(self, name, key):
        pipeline = self._private_message_pipelines.get(name)
        if not isinstance(pipeline, dict):
            return False
        messages = pipeline.get("open_messages") or []
        kept = [message for message in messages if getattr(message, "_wxbot_pending_voice_key", "") != key]
        if len(kept) == len(messages):
            return False
        pipeline["open_messages"] = kept
        if not kept:
            pipeline["open_started_at"] = 0.0
            pipeline["open_kind"] = "text"
        return True

    def _replace_pending_group_voice_placeholder_locked(self, name, key, resolved):
        pipeline = self._group_message_pipelines.get(name)
        if not isinstance(pipeline, dict):
            return False
        messages = pipeline.get("messages")
        if not isinstance(messages, deque):
            return False
        for index, message in enumerate(messages):
            if getattr(message, "_wxbot_pending_voice_key", "") != key:
                continue
            resolved._wxbot_pending_voice_key = key
            resolved._wxbot_pending_voice_resolved = True
            resolved._wxbot_media_prepared = True
            resolved._wxbot_persisted = True
            messages[index] = resolved
            self._start_group_message_worker_locked(pipeline)
            return True
        return False

    def _drop_pending_group_voice_placeholder_locked(self, name, key):
        pipeline = self._group_message_pipelines.get(name)
        if not isinstance(pipeline, dict):
            return False
        messages = pipeline.get("messages")
        if not isinstance(messages, deque):
            return False
        kept = deque(
            message
            for message in messages
            if getattr(message, "_wxbot_pending_voice_key", "") != key
        )
        if len(kept) == len(messages):
            return False
        pipeline["messages"] = kept
        self._start_group_message_worker_locked(pipeline)
        return True

    def _wake_private_batch_if_pending_voice_unblocked_locked(self, name, chat):
        pipeline = self._private_message_pipelines.get(name)
        if not isinstance(pipeline, dict):
            return False
        if not pipeline.get("pending_voice_blocked_close"):
            return False
        if self._private_pipeline_has_unresolved_voice_locked(pipeline):
            return False
        pipeline["pending_voice_blocked_close"] = False
        if not pipeline.get("open_messages"):
            return False
        self._cancel_timer(pipeline.get("idle_timer"))
        self._cancel_timer(pipeline.get("max_timer"))
        pipeline["idle_timer"] = self._schedule_private_message_timer(
            0,
            self._close_private_message_batch_by_idle,
            chat,
        )
        pipeline["max_timer"] = None
        return True

    def _cancel_pending_private_voice_transcription_locked(self, chat_name):
        pending = getattr(self, "_pending_private_voice_transcription", None)
        if not isinstance(pending, dict):
            return False
        task = pending.pop(str(chat_name or "").strip(), None)
        if not isinstance(task, dict):
            return False
        self._cancel_timer(task.get("timer"))
        return True

    def _reschedule_pending_private_voice_transcription(self, chat, items, *, reason):
        name = str(getattr(chat, "who", "") or "").strip()
        if not name or not items or self.is_stop_requested():
            return True
        task_key = self._pending_voice_task_key(chat)
        self._ensure_pending_private_voice_transcription_state()
        with self._chat_merge_lock:
            task = self._pending_private_voice_transcription.get(task_key)
            if not isinstance(task, dict):
                task = {"chat": chat, "items": {}, "timer": None}
                self._pending_private_voice_transcription[task_key] = task
            task["chat"] = chat
            for item in items:
                key = item.get("key")
                if not key:
                    continue
                task["items"][key] = item
                source_key = item.get("source_key")
                if source_key:
                    task.setdefault("source_keys", set()).add(source_key)
            if not task.get("timer"):
                task["timer"] = self._schedule_private_message_timer(
                    VOICE_TRANSCRIPTION_RETRY_DELAY_SECONDS,
                    self._flush_pending_private_voice_transcription,
                    chat,
                )
        return True

    def _queue_pending_private_voice_transcription(self, chat, message):
        name = str(getattr(chat, "who", "") or "").strip()
        if not name:
            return False
        self._ensure_pending_private_voice_transcription_state()
        source_key = message_unique_id(name, message)
        chat_ref = ConversationRef.from_wx_chat(chat)
        task_key = self._pending_voice_task_key(chat_ref)
        is_group = chat_ref.chat_type == "group"
        event_id = str(getattr(message, "_wxbot_event_id", "") or "").strip()
        item = {
            "key": "",
            "source_key": source_key,
            "message": MessageEnvelope.from_wx_message(
                message,
                ingress_source=str(getattr(message, "_wxbot_ingress_source", "subwindow") or "subwindow"),
                received_at=time.time(),
            ),
            "signature": {
                "attr": str(getattr(message, "attr", "") or ""),
                "sender": str(getattr(message, "sender", "") or ""),
                "duration": message_routing.voice_duration_seconds(getattr(message, "content", "")),
                "hash": getattr(message, "hash", ""),
            },
            "first_seen_at": time.time(),
            "reread_attempts": 0,
            "event_id": event_id,
            "event_version": int(getattr(message, "_wxbot_event_version", 0) or 0),
            "reply_expires_at": float(getattr(message, "_wxbot_reply_expires_at", 0.0) or 0.0),
            "chat_type": chat_ref.chat_type,
        }
        created_task = False
        with self._chat_merge_lock:
            task = self._pending_private_voice_transcription.get(task_key)
            if not isinstance(task, dict):
                task = {"chat": chat_ref, "items": {}, "source_keys": set(), "timer": None}
                self._pending_private_voice_transcription[task_key] = task
            task.setdefault("source_keys", set())
            already_pending = bool(source_key and source_key in task["source_keys"])
            if already_pending:
                task["chat"] = chat_ref
                return True
            sequence = self._pending_private_voice_sequence.get(task_key, 0) + 1
            self._pending_private_voice_sequence[task_key] = sequence
            key = f"voice:{name}:{sequence}"
            item["key"] = key
            task["chat"] = chat_ref
            if source_key:
                task["source_keys"].add(source_key)
            task["items"][key] = item
            if is_group:
                setattr(message, "_wxbot_pending_voice_key", key)
                setattr(message, "_wxbot_pending_voice_resolved", False)
            else:
                self._insert_pending_private_voice_placeholder_locked(name, chat, message, key)
            if not task.get("timer"):
                task["timer"] = self._schedule_private_message_timer(
                    VOICE_TRANSCRIPTION_RETRY_DELAY_SECONDS,
                    self._flush_pending_private_voice_transcription,
                    chat_ref,
                )
                created_task = True
        return True

    def _read_pending_voice_snapshot(self, chat):
        conversation = ConversationRef.from_wx_chat(chat)
        name = conversation.who
        if not name:
            return []
        get_subwindow = getattr(getattr(self, "wx", None), "GetSubWindow", None)
        if not callable(get_subwindow):
            return []
        current_chat = get_subwindow(
            nickname=name,
            chat_type=conversation.chat_type,
        )
        if current_chat is None:
            return []
        get_messages = getattr(current_chat, "GetAllMessage", None)
        if not callable(get_messages):
            return []
        raw_messages = list(get_messages() or [])
        return [
            MessageEnvelope.from_wx_message(message, ingress_source="voice_snapshot", window_order=index)
            for index, message in enumerate(raw_messages)
        ]

    def _flush_pending_private_voice_transcription(self, chat):
        name = str(getattr(chat, "who", "") or "").strip()
        if not name:
            return True
        chat_ref = ConversationRef.from_wx_chat(chat)
        task_key = self._pending_voice_task_key(chat_ref)
        is_group = chat_ref.chat_type == "group"
        scene_label = "群聊" if is_group else "私聊"
        self._ensure_pending_private_voice_transcription_state()
        with self._chat_merge_lock:
            task = self._pending_private_voice_transcription.pop(task_key, None)
            if isinstance(task, dict):
                task["timer"] = None
        if not isinstance(task, dict):
            return True
        items = list((task.get("items") or {}).values())
        if not items or self.is_stop_requested():
            return True
        try:
            reread_items = [
                item
                for item in items
                if message_routing.voice_content_state(
                    getattr(item.get("message"), "content", "")
                ) != "valid"
            ]
            for item in reread_items:
                item["reread_attempts"] = int(item.get("reread_attempts") or 0) + 1
            if reread_items:
                snapshot = self._read_pending_voice_snapshot(chat_ref)
                matched = message_routing.match_pending_voice_snapshot(reread_items, snapshot)
                for item in reread_items:
                    resolved = matched.get(item.get("key"))
                    if resolved is not None:
                        item["message"] = resolved
        except Exception as exc:
            expired = [
                item for item in items
                if self._pending_voice_item_expired(item) or self._pending_voice_item_deadline_reached(item)
            ]
            pending = [item for item in items if item not in expired]
            if pending:
                self._reschedule_pending_private_voice_transcription(chat, pending, reason=f"重读失败：{exc}")
            if expired:
                terminalized = self._terminalize_exhausted_voice_recovery(expired)
                with self._chat_merge_lock:
                    for item in expired:
                        item_key = item.get("key")
                        if item_key:
                            if is_group:
                                self._drop_pending_group_voice_placeholder_locked(name, item_key)
                            else:
                                self._drop_pending_private_voice_placeholder_locked(name, item_key)
                    if not is_group:
                        self._wake_private_batch_if_pending_voice_unblocked_locked(name, chat)
                log(message=(
                    f"{scene_label} {name}：{len(expired)} 条语音重读仍未得到有效文字；"
                    f"{terminalized} 条已达到恢复上限并结束，最后一次重读失败：{exc}"
                ))
            return True
        pending_items = []
        expired_items = []
        for item in items:
            item_key = item.get("key")
            resolved = item.get("message")
            if not resolved:
                if self._pending_voice_item_expired(item) or self._pending_voice_item_deadline_reached(item):
                    expired_items.append(item)
                    if item_key:
                        with self._chat_merge_lock:
                            if is_group:
                                self._drop_pending_group_voice_placeholder_locked(name, item_key)
                            else:
                                self._drop_pending_private_voice_placeholder_locked(name, item_key)
                                self._wake_private_batch_if_pending_voice_unblocked_locked(name, chat)
                else:
                    pending_items.append(item)
                continue
            state = message_routing.voice_content_state(getattr(resolved, "content", ""))
            if state == "valid":
                event_id = str(item.get("event_id") or "").strip()
                resolved._wxbot_event_id = event_id
                resolved._wxbot_event_ids = (event_id,) if event_id else ()
                resolved._wxbot_event_version = int(item.get("event_version", 0) or 0)
                resolved._wxbot_reply_expires_at = float(item.get("reply_expires_at", 0.0) or 0.0)
                resolved._wxbot_media_prepared = True
                resolved._wxbot_pending_voice_resolved = True
                if event_id and self._message_store is not None:
                    try:
                        self._message_store.update_inbound_content(
                            event_id,
                            strip_voice_duration_metadata(getattr(resolved, "content", "")),
                        )
                    except Exception as exc:
                        retry_delay = self._reply_retry_delay(resolved, exc)
                        if retry_delay is not None:
                            pending_items.append(item)
                            log(level="WARNING", message=f"{scene_label} {name}：语音转写写库繁忙，稍后重试：{exc}")
                            continue
                        cleanup_error = self._cancel_failed_reply_attempt(resolved, str(exc))
                        suffix = f"；取消失败：{cleanup_error}" if cleanup_error else ""
                        if item_key:
                            with self._chat_merge_lock:
                                if is_group:
                                    self._drop_pending_group_voice_placeholder_locked(name, item_key)
                                else:
                                    self._drop_pending_private_voice_placeholder_locked(name, item_key)
                                    self._wake_private_batch_if_pending_voice_unblocked_locked(name, chat)
                        log(level="ERROR", message=f"{scene_label} {name}：语音转写写库失败：{exc}{suffix}")
                        continue
                if is_group:
                    with self._chat_merge_lock:
                        replaced = self._replace_pending_group_voice_placeholder_locked(
                            name,
                            item_key,
                            resolved,
                        )
                    if not replaced:
                        self._terminalize_exhausted_voice_recovery([item])
                        log(message=f"群聊 {name}：语音识别结果已过期，已静默忽略")
                    continue
                if str((item.get("signature") or {}).get("attr") or "") != "friend":
                    log(message=f"私聊 {name}：self 语音识别结果已补入历史")
                    continue
                replaced = False
                if item_key:
                    with self._chat_merge_lock:
                        replaced = self._replace_pending_private_voice_placeholder_locked(name, item_key, resolved)
                        self._wake_private_batch_if_pending_voice_unblocked_locked(name, chat)
                if not replaced:
                    self._terminalize_exhausted_voice_recovery([item])
                    log(message=f"私聊 {name}：语音识别结果已过期，已静默忽略")
                    continue
            elif state == "failed":
                if item_key:
                    with self._chat_merge_lock:
                        if is_group:
                            self._drop_pending_group_voice_placeholder_locked(name, item_key)
                        else:
                            self._drop_pending_private_voice_placeholder_locked(name, item_key)
                            self._wake_private_batch_if_pending_voice_unblocked_locked(name, chat)
                self._terminalize_exhausted_voice_recovery([item])
                log(message=f"{scene_label} {name}：语音识别明确失败，未触发回复")
            elif self._pending_voice_item_expired(item) or self._pending_voice_item_deadline_reached(item):
                expired_items.append(item)
                if item_key:
                    with self._chat_merge_lock:
                        if is_group:
                            self._drop_pending_group_voice_placeholder_locked(name, item_key)
                        else:
                            self._drop_pending_private_voice_placeholder_locked(name, item_key)
                            self._wake_private_batch_if_pending_voice_unblocked_locked(name, chat)
            else:
                pending_items.append(item)
        if pending_items:
            self._reschedule_pending_private_voice_transcription(chat, pending_items, reason="结果仍未就绪")
        if expired_items:
            terminalized = self._terminalize_exhausted_voice_recovery(expired_items)
            log(message=(
                f"{scene_label} {name}：{len(expired_items)} 条语音重读 {VOICE_TRANSCRIPTION_MAX_REREAD_ATTEMPTS} 次仍未得到有效文字；"
                f"{terminalized} 条已达到恢复上限并结束，其他记录留待下次启动补历史"
            ))
        return True

    def _mark_context_repair_needed_after_restore(self, chat_name, *, chat_type):
        chat_name = str(chat_name or "").strip()
        chat_type = str(chat_type or "").strip().lower()
        if not chat_name or chat_type not in {"private", "group"}:
            return False
        self._ensure_message_runtime_state()
        with self._memory_context_repair_lock:
            self._memory_context_repair_state[f"{chat_type}:{chat_name}"] = {
                "dirty": True,
                "retry_at": 0.0,
            }
        return True

    def _context_repair_should_run(self, repair_key):
        self._ensure_message_runtime_state()
        with self._memory_context_repair_lock:
            state = self._memory_context_repair_state.get(repair_key)
            if state is None:
                return True
            return bool(state.get("dirty", True)) and time.time() >= float(
                state.get("retry_at", 0.0) or 0.0
            )

    def _finish_context_repair_attempt(self, repair_key, *, success):
        self._ensure_message_runtime_state()
        with self._memory_context_repair_lock:
            self._memory_context_repair_state[repair_key] = {
                "dirty": not success,
                "retry_at": 0.0 if success else time.time() + DEFAULT_CONTEXT_REPAIR_RETRY_SECONDS,
            }

    def _read_visible_context_messages(self, chat, limit):
        get_all = getattr(chat, "GetAllMessage", None)
        if not callable(get_all):
            raise RuntimeError("当前聊天窗口不支持 GetAllMessage")
        with warn_slow_wechat_ui_action(
            f"上下文补洞 GetAllMessage({getattr(chat, 'who', '')})",
            level="WARNING",
        ):
            messages = list(get_all() or [])
        limit = max(1, min(50, int(limit or DEFAULT_VISIBLE_LIMIT)))
        selected = []
        message_count = 0
        for message in reversed(messages):
            is_time_separator = (
                str(getattr(message, "type", "") or "").strip().lower() == "time"
            )
            if not is_time_separator:
                if message_count >= limit:
                    break
                message_count += 1
            selected.append(message)
        return list(reversed(selected))

    def _context_repair_log_label(self, chat_type, chat_name):
        label = "群聊" if str(chat_type or "").strip().lower() == "group" else "私聊"
        return f"{label} {chat_name}"

    def _log_context_repair_result(self, chat_type, chat_name, source, added, *, suffix=""):
        label = self._context_repair_log_label(chat_type, chat_name)
        action_text = {
            "ui": "上下文 UI 补洞",
        }.get(source, "上下文补洞")
        suffix = str(suffix or "").strip()
        suffix_text = f"，{suffix}" if suffix else ""
        added = int(added or 0)
        level = "DEBUG" if added == 0 and not suffix else "INFO"
        log(level=level, message=f"{label}：{action_text}完成，补入 {added} 条{suffix_text}")

    def _repair_context_before_ai(self, chat, message, *, chat_type):
        chat_name = str(getattr(chat, "who", "") or "").strip()
        chat_type = str(chat_type or "").strip().lower()
        if chat_type not in {"private", "group"}:
            raise ValueError("chat_type must be private or group")
        if str(getattr(chat, "chat_type", "") or "").strip().lower() != chat_type:
            return []
        switch_name = (
            "group_context_repair_switch"
            if chat_type == "group"
            else "chat_context_repair_switch"
        )
        if not (
            getattr(getattr(self, "config", None), "memory_switch", False)
            and getattr(getattr(self, "config", None), switch_name, True)
            and self.memory_manager
        ):
            return []
        if not chat_name:
            return []
        repair_key = f"{chat_type}:{chat_name}"
        if not self._context_repair_should_run(repair_key):
            return []

        try:
            visible_messages = self._read_visible_context_messages(chat, DEFAULT_VISIBLE_LIMIT)
            boundary = snapshot_before_current(
                visible_messages,
                message,
                chat_type=chat_type,
            )
            if not boundary.found:
                self._finish_context_repair_attempt(repair_key, success=False)
                label = self._context_repair_log_label(chat_type, chat_name)
                log(level="INFO", message=f"{label}：上下文补洞未能确认当前消息边界，稍后重试")
                return []
            visible_entries = normalize_wechat_snapshot(
                boundary.messages,
                source="wechat_context_repair",
            )
            if not visible_entries:
                self._finish_context_repair_attempt(repair_key, success=True)
                self._log_context_repair_result(chat_type, chat_name, "ui", 0)
                return []
            current_event_ids = self._reply_event_ids(message)
            if not current_event_ids:
                raise RuntimeError("当前消息尚未写入事实库")
            result = self.memory_manager.reconcile_visible_tail(
                chat_name,
                visible_entries,
                current_event_ids=current_event_ids,
                chat_type=chat_type,
                history_limit=DEFAULT_LOCAL_HISTORY_LIMIT,
            )
            added = int(result.get("added", 0) or 0)
            anchor_found = bool(result.get("anchor_found"))
            deleted_boundary_skipped = int(result.get("deleted_boundary_skipped", 0) or 0)
            suffix_parts = []
            if not anchor_found:
                if deleted_boundary_skipped:
                    suffix_parts.append("无历史重合且存在删除记录，已跳过当前可见尾段")
                else:
                    suffix_parts.append("无历史重合，已按最新记录补入")
            if deleted_boundary_skipped:
                suffix_parts.append(f"{deleted_boundary_skipped} 条未重新补入")
            self._log_context_repair_result(
                chat_type,
                chat_name,
                "ui",
                added,
                suffix="，".join(suffix_parts),
            )
            if added:
                self._mark_chat_memory_dirty(
                    SimpleNamespace(who=chat_name, chat_type=chat_type),
                    SimpleNamespace(type="text", attr="friend", content="[上下文补洞]"),
                )
            self._finish_context_repair_attempt(repair_key, success=True)
            return list(result.get("history_messages") or [])
        except Exception as exc:
            self._finish_context_repair_attempt(repair_key, success=False)
            label = self._context_repair_log_label(chat_type, chat_name)
            log(level="WARNING", message=f"{label}：上下文补洞失败，已继续原回复流程，详情：{exc}")
            return []

    def _build_merged_private_message(self, messages):
        source_messages = list(messages or [])
        merged = build_merged_private_message(
            source_messages,
            on_extra_image=lambda _image_path: log(
                level="INFO",
                message=f"私聊连续消息收到超过 {MAX_MERGED_PRIVATE_IMAGES} 张图片，超出部分已忽略",
            ),
        )
        event_ids = []
        versions = []
        expiries = []
        for message in source_messages:
            for event_id in getattr(message, "_wxbot_event_ids", ()) or ():
                event_id = str(event_id or "").strip()
                if event_id and event_id not in event_ids:
                    event_ids.append(event_id)
            version = int(getattr(message, "_wxbot_event_version", 0) or 0)
            if version:
                versions.append(version)
            expiry = float(getattr(message, "_wxbot_reply_expires_at", 0.0) or 0.0)
            if expiry:
                expiries.append(expiry)
        merged._wxbot_event_ids = tuple(event_ids)
        merged._wxbot_event_version = max(versions, default=0)
        merged._wxbot_reply_expires_at = max(expiries, default=0.0)
        merged._wxbot_business_retry_count = max(
            (
                int(getattr(message, "_wxbot_business_retry_count", 0) or 0)
                for message in source_messages
            ),
            default=0,
        )
        return merged

    def _private_message_merge_delay(self):
        return getattr(self.config, 'chat_message_merge_delay', 20)

    @staticmethod
    def _private_message_max_wait(delay):
        return min(180.0, float(delay) * 3.0)

    @staticmethod
    def _private_message_effective_merge_delay(pipeline, base_delay):
        kind = str((pipeline or {}).get("open_kind") or "text").strip().lower()
        multiplier = 2.0 if kind in {"image", "mixed"} else 1.0
        return min(120.0, float(base_delay) * multiplier)

    def _private_message_pipeline(self, chat_name):
        self._ensure_message_runtime_state()
        name = str(chat_name or "").strip()
        if not name:
            return None
        pipeline = self._private_message_pipelines.get(name)
        if not isinstance(pipeline, dict):
            pipeline = {
                "open_messages": [],
                "open_started_at": 0.0,
                "open_kind": "text",
                "idle_timer": None,
                "max_timer": None,
                "queued_batches": deque(),
                "worker_running": False,
            }
            self._private_message_pipelines[name] = pipeline
        if not isinstance(pipeline.get("queued_batches"), deque):
            pipeline["queued_batches"] = deque(pipeline.get("queued_batches") or [])
        if not isinstance(pipeline.get("open_messages"), list):
            pipeline["open_messages"] = []
        if str(pipeline.get("open_kind", "") or "").strip() not in {"text", "image", "mixed"}:
            pipeline["open_kind"] = "text"
        return pipeline

    @staticmethod
    def _private_message_batch_kind(message):
        msg_type = str(getattr(message, "type", "") or "").strip().lower()
        content = str(getattr(message, "content", "") or "")
        if msg_type == "image":
            return "image"
        if msg_type == "text" and QUOTE_IMAGE_MARKER in content:
            return "image"
        return "text"

    @staticmethod
    def _cancel_timer(timer):
        if not timer:
            return
        try:
            timer.cancel()
        except Exception:
            pass

    def _schedule_private_message_timer(self, seconds, callback, chat):
        timer = threading.Timer(max(0.0, float(seconds or 0.0)), callback, args=(chat,))
        timer.daemon = True
        timer.start()
        return timer

    def _private_message_pipeline_has_work_locked(self, chat_name):
        pipelines = getattr(self, "_private_message_pipelines", {})
        pipeline = pipelines.get(str(chat_name or "").strip()) if isinstance(pipelines, dict) else None
        return isinstance(pipeline, dict) and (
            bool(pipeline.get("open_messages"))
            or bool(pipeline.get("queued_batches"))
            or bool(pipeline.get("worker_running"))
        )

    def _schedule_private_message_pipeline_locked(self, chat, pipeline, delay, max_wait_base_delay=None):
        self._cancel_timer(pipeline.get("idle_timer"))
        pipeline["idle_timer"] = self._schedule_private_message_timer(
            delay,
            self._close_private_message_batch_by_idle,
            chat,
        )
        if not pipeline.get("max_timer"):
            elapsed = max(0.0, time.time() - float(pipeline.get("open_started_at") or time.time()))
            max_wait = self._private_message_max_wait(max_wait_base_delay if max_wait_base_delay is not None else delay)
            pipeline["max_timer"] = self._schedule_private_message_timer(
                max(0.0, max_wait - elapsed),
                self._close_private_message_batch_by_max_wait,
                chat,
            )

    def _enqueue_private_message_batch_locked(self, pipeline, messages):
        msgs = list(messages or [])
        if not msgs:
            return False
        batches = pipeline["queued_batches"]
        if len(batches) >= PRIVATE_MESSAGE_PIPELINE_MAX_QUEUED_BATCHES:
            batches[-1].extend(msgs)
            return True
        batches.append(msgs)
        return True

    def _close_private_message_batch_locked(self, chat, reason="idle"):
        name = str(getattr(chat, "who", "") or "").strip()
        pipeline = self._private_message_pipeline(name)
        if not pipeline:
            return False
        messages = list(pipeline.get("open_messages") or [])
        if not messages:
            pipeline["open_started_at"] = 0.0
            self._cancel_timer(pipeline.get("idle_timer"))
            self._cancel_timer(pipeline.get("max_timer"))
            pipeline["idle_timer"] = None
            pipeline["max_timer"] = None
            return False
        if self._private_pipeline_has_unresolved_voice_locked(pipeline):
            pipeline["pending_voice_blocked_close"] = True
            self._cancel_timer(pipeline.get("idle_timer"))
            self._cancel_timer(pipeline.get("max_timer"))
            pipeline["idle_timer"] = None
            pipeline["max_timer"] = None
            return False
        pipeline["open_messages"] = []
        pipeline["open_started_at"] = 0.0
        pipeline["open_kind"] = "text"
        pipeline["pending_voice_blocked_close"] = False
        self._cancel_timer(pipeline.get("idle_timer"))
        self._cancel_timer(pipeline.get("max_timer"))
        pipeline["idle_timer"] = None
        pipeline["max_timer"] = None
        self._enqueue_private_message_batch_locked(pipeline, messages)
        self._start_private_message_worker_locked(chat, pipeline)
        if reason == "max_wait":
            log(level="DEBUG", message=f"私聊 {name}：连续消息达到最大等待，已先处理当前批次")
        return True

    def _start_private_message_worker_locked(self, chat, pipeline):
        if pipeline.get("worker_running"):
            return False
        if not pipeline.get("queued_batches"):
            return False
        pipeline["worker_running"] = True
        worker = threading.Thread(target=self._run_private_message_pipeline_worker, args=(chat,))
        worker.daemon = True
        worker.start()
        return True

    def _schedule_private_message_retry(self, chat, messages, delay):
        name = str(getattr(chat, "who", "") or "").strip()

        def requeue():
            if self.is_stop_requested():
                return
            with self._chat_merge_lock:
                pipeline = self._private_message_pipelines.get(name)
                if not isinstance(pipeline, dict):
                    return
                pipeline["queued_batches"].appendleft(list(messages))
                self._start_private_message_worker_locked(chat, pipeline)

        timer = threading.Timer(max(0.0, float(delay or 0.0)), requeue)
        timer.daemon = True
        timer.start()

    def _close_private_message_batch_by_idle(self, chat):
        try:
            if self.is_stop_requested():
                self._clear_private_message_pipeline(getattr(chat, "who", ""))
                return True
            with self._chat_merge_lock:
                return self._close_private_message_batch_locked(chat, reason="idle")
        except Exception as exc:
            log(level="ERROR", message=f"私聊连续消息空闲关闭失败：{exc}\n{traceback.format_exc()}")
            self._clear_private_message_pipeline(getattr(chat, "who", ""))
            return False

    def _close_private_message_batch_by_max_wait(self, chat):
        try:
            if self.is_stop_requested():
                self._clear_private_message_pipeline(getattr(chat, "who", ""))
                return True
            with self._chat_merge_lock:
                return self._close_private_message_batch_locked(chat, reason="max_wait")
        except Exception as exc:
            log(level="ERROR", message=f"私聊连续消息最大等待关闭失败：{exc}\n{traceback.format_exc()}")
            self._clear_private_message_pipeline(getattr(chat, "who", ""))
            return False

    def _clear_private_message_pipeline(self, chat_name):
        self._ensure_message_runtime_state()
        name = str(chat_name or "").strip()
        if not name:
            return None
        with self._chat_merge_lock:
            pipeline = self._private_message_pipelines.pop(name, None)
        if not isinstance(pipeline, dict):
            return None
        self._cancel_timer(pipeline.get("idle_timer"))
        self._cancel_timer(pipeline.get("max_timer"))
        return pipeline

    def _run_private_message_pipeline_worker(self, chat):
        name = str(getattr(chat, "who", "") or "").strip()
        if not name:
            return True
        com_ready = False
        pythoncom_module = globals().get("pythoncom", None)
        if pythoncom_module is not None:
            try:
                pythoncom_module.CoInitialize()
                com_ready = True
            except Exception as exc:
                log(level="WARNING", message=f"私聊连续消息线程初始化 COM 失败：{exc}")
        try:
            while not self.is_stop_requested():
                with self._chat_merge_lock:
                    pipeline = self._private_message_pipeline(name)
                    if not pipeline or not pipeline.get("queued_batches"):
                        if pipeline:
                            pipeline["worker_running"] = False
                        return True
                    messages = list(pipeline["queued_batches"].popleft())
                if not messages:
                    continue
                merged = self._build_merged_private_message(messages)
                if not str(getattr(merged, 'content', '') or '').strip():
                    continue
                try:
                    self.wx_send_ai(chat, merged)
                except Exception as exc:
                    retry_delay = self._reply_retry_delay(merged, exc)
                    if retry_delay is not None:
                        retry_count = int(
                            getattr(merged, "_wxbot_business_retry_count", 0) or 0
                        )
                        for message in messages:
                            message._wxbot_business_retry_count = retry_count
                        log(level="WARNING", message=f"私聊消息库暂时繁忙，稍后重试：{exc}")
                        with self._chat_merge_lock:
                            pipeline = self._private_message_pipelines.get(name)
                            if isinstance(pipeline, dict):
                                pipeline["worker_running"] = False
                        self._schedule_private_message_retry(chat, messages, retry_delay)
                        return True
                    cleanup_error = self._cancel_failed_reply_attempt(merged, str(exc))
                    suffix = f"；取消失败：{cleanup_error}" if cleanup_error else ""
                    log(
                        level="ERROR",
                        message=f"私聊连续消息处理失败：{exc}{suffix}\n{traceback.format_exc()}",
                    )
                    continue
            self._clear_private_message_pipeline(name)
            return True
        finally:
            if com_ready:
                try:
                    pythoncom_module.CoUninitialize()
                except Exception:
                    pass

    def _enqueue_private_message_for_ai(self, chat, message):
        self._ensure_message_runtime_state()
        if self.is_stop_requested():
            return True
        is_recovery = bool(getattr(message, "_wxbot_startup_recovery", False))
        if getattr(message, '_voice_transcription_failed', False):
            log(message=f"私聊 {chat.who}：语音识别失败，未得到有效文字，已静默忽略")
            self._mark_inbound_no_reply(message)
            return True
        if self._should_skip_private_ai_message(message):
            if not str(getattr(message, "_wxbot_pending_voice_key", "") or "").strip():
                self._mark_inbound_no_reply(message)
            return True
        if is_recovery:
            with self._chat_merge_lock:
                pipeline = self._private_message_pipeline(chat.who)
                if not pipeline:
                    return True
                if self._private_pipeline_has_unresolved_voice_locked(pipeline):
                    batch_kind = self._private_message_batch_kind(message)
                    open_kind = str(pipeline.get("open_kind") or "text").strip().lower()
                    pipeline["open_kind"] = open_kind if open_kind == batch_kind else "mixed"
                    pipeline["open_messages"].append(message)
                    return True
                self._enqueue_private_message_batch_locked(pipeline, [message])
                self._start_private_message_worker_locked(chat, pipeline)
            return True
        base_delay = self._private_message_merge_delay()
        batch_kind = self._private_message_batch_kind(message)
        pending_image_paths = []
        with self._chat_merge_lock:
            pipeline = self._private_message_pipeline(chat.who)
            if not pipeline:
                return True
            open_kind = str(pipeline.get("open_kind") or "text").strip().lower()
            if not pipeline["open_messages"]:
                pipeline["open_started_at"] = time.time()
                pipeline["open_kind"] = batch_kind
            else:
                pipeline["open_kind"] = open_kind if open_kind == batch_kind else "mixed"
            pipeline["open_messages"].append(message)
            if getattr(self.config, 'chat_image_recognition_switch', False):
                for queued_message in pipeline["open_messages"]:
                    pending_image_paths.extend(self._extract_message_image_paths(queued_message))
            delay = self._private_message_effective_merge_delay(pipeline, base_delay)
            self._schedule_private_message_pipeline_locked(chat, pipeline, delay, max_wait_base_delay=base_delay)
        if pending_image_paths:
            self._set_pending_visual_context(
                chat.who,
                pending_image_paths,
                chat_type="private",
            )
        return True

    @staticmethod
    def _split_reply_text_length(part_text):
        text = re.sub(r"\s+", "", str(part_text or ""))
        return len(text)

    def _split_reply_delay_seconds(self, part_text, *, is_last=False):
        text_len = self._split_reply_text_length(part_text)
        if text_len <= 10:
            lo, hi = 0.8, 1.8
        elif text_len <= 20:
            lo, hi = 1.5, 3.0
        else:
            lo, hi = 2.5, 4.5
        delay = random.uniform(lo, hi)
        if is_last:
            delay += random.uniform(0.3, 1.0)
        delay *= 2.0
        return min(24.0, max(0.5, delay))

    def _human_delay_for_reply_part(
        self,
        *,
        part_text="",
        split_continuation=False,
        is_last=False,
        delay_enabled=True,
    ):
        if not split_continuation or not delay_enabled:
            return
        self._wait_or_stop_requested(self._split_reply_delay_seconds(part_text, is_last=is_last))

    def _inter_message_delay_or_stop(self):
        self._wait_or_stop_requested(self._split_reply_delay_seconds("", is_last=False))

    @staticmethod
    def _remove_temp_audio_file(audio_path):
        target = str(audio_path or "").strip()
        if not target:
            return
        try:
            if os.path.isfile(target):
                os.remove(target)
        except Exception:
            pass

    def _log_reply_split_outcome(self, *, scene_label, chat_name, split_source, split_count):
        if split_source == "newline":
            log(level="DEBUG", message=f"{scene_label} {chat_name}：命中换行，已自动拆分成 {split_count} 条")
            return
        if split_source == "sentence":
            log(level="DEBUG", message=f"{scene_label} {chat_name}：命中句末标点，已自动拆分成 {split_count} 条")
            return
        if split_source == "space":
            log(level="DEBUG", message=f"{scene_label} {chat_name}：命中中文空格停顿，已自动拆分成 {split_count} 条")
            return

    def _log_empty_cleaned_reply(self):
        fallback_reply = str(getattr(self.config, "reply_preprocess_fallback_reply", "") or "").strip()
        if fallback_reply:
            log(level="WARNING", message="AI 回复清洗后为空，已使用异常兜底回复")
        else:
            log(level="WARNING", message="AI 回复清洗后为空，已按规则静默跳过发送")

    @staticmethod
    def _normalize_visual_image_paths(image_paths):
        return [
            str(path or "").strip()
            for path in (image_paths or [])
            if str(path or "").strip()
        ]

    @staticmethod
    def _normalize_visual_note_slots(image_paths, visual_notes):
        notes = [str(note or "").strip() for note in (visual_notes or [])]
        return [
            notes[index] if index < len(notes) else ""
            for index, _path in enumerate(image_paths or [])
        ]

    @staticmethod
    def _normalize_visual_sender_slots(image_paths, senders, *, fallback=""):
        values = [str(sender or "").strip() for sender in (senders or [])]
        fallback = str(fallback or "").strip()
        return [
            values[index] if index < len(values) and values[index] else fallback
            for index, _path in enumerate(image_paths or [])
        ]

    @staticmethod
    def _visual_context_key(chat_name, chat_type):
        conversation = ConversationRef(chat_name, chat_type)
        if not conversation.who:
            raise ValueError("visual context conversation must not be empty")
        return conversation.chat_type, conversation.who

    @staticmethod
    def _text_references_pending_visual_context(text):
        text = str(text or "")
        return bool(
            PENDING_VISUAL_DIRECT_REFERENCE_RE.search(text)
            or PENDING_VISUAL_STANDALONE_ACTION_RE.search(text)
            or (
                PENDING_VISUAL_CONTEXT_REFERENCE_RE.search(text)
                and PENDING_VISUAL_ACTION_RE.search(text)
            )
        )

    def _set_pending_visual_context(
        self,
        chat_name,
        image_paths,
        *,
        chat_type,
        senders=None,
        visual_notes=None,
        append=False,
        observed_at=None,
    ):
        self._ensure_message_runtime_state()
        key = self._visual_context_key(chat_name, chat_type)
        normalized_paths = self._normalize_visual_image_paths(image_paths)
        if not normalized_paths:
            self._clear_pending_visual_context(chat_name, chat_type=chat_type)
            return None
        normalized_notes = self._normalize_visual_note_slots(normalized_paths, visual_notes)
        normalized_senders = self._normalize_visual_sender_slots(normalized_paths, senders)
        now = time.time() if observed_at is None else float(observed_at)
        if append:
            with self._chat_merge_lock:
                existing = dict(self._pending_visual_contexts.get(key) or {})
                if existing and float(existing.get("expires_at", 0.0) or 0.0) < time.time():
                    self._pending_visual_contexts.pop(key, None)
                    existing = None
            if existing:
                existing_paths = self._normalize_visual_image_paths(existing.get("image_paths"))
                existing_notes = self._normalize_visual_note_slots(existing_paths, existing.get("visual_notes"))
                existing_senders = self._normalize_visual_sender_slots(
                    existing_paths,
                    existing.get("image_senders"),
                )
                same_group_batch = (
                    key[0] != "group"
                    or (
                        not any(existing_notes)
                        and now - float(existing.get("last_image_at", 0.0) or 0.0)
                        <= GROUP_VISUAL_BATCH_GAP_SECONDS
                    )
                )
                if same_group_batch:
                    combined = list(zip(
                        existing_paths + normalized_paths,
                        existing_senders + normalized_senders,
                        existing_notes + normalized_notes,
                    ))[-MAX_MERGED_PRIVATE_IMAGES:]
                    normalized_paths = [item[0] for item in combined]
                    normalized_senders = [item[1] for item in combined]
                    normalized_notes = [item[2] for item in combined]
        else:
            normalized_paths = normalized_paths[-MAX_MERGED_PRIVATE_IMAGES:]
            normalized_senders = normalized_senders[-MAX_MERGED_PRIVATE_IMAGES:]
            normalized_notes = normalized_notes[-MAX_MERGED_PRIVATE_IMAGES:]
        context = {
            "image_paths": normalized_paths,
            "image_senders": normalized_senders,
            "visual_notes": normalized_notes,
            "last_image_at": now,
            "expires_at": now + (
                GROUP_PENDING_VISUAL_CONTEXT_TTL_SECONDS
                if key[0] == "group"
                else PRIVATE_PENDING_VISUAL_CONTEXT_TTL_SECONDS
            ),
        }
        with self._chat_merge_lock:
            self._pending_visual_contexts[key] = context
        return dict(context)

    def _restore_group_pending_visual_context(self, chat_name):
        store = getattr(self, "_message_store", None)
        recent_images = getattr(store, "recent_image_events", None)
        if not callable(recent_images):
            return None
        now = time.time()
        events = recent_images(
            chat_name,
            chat_type="group",
            since=now - GROUP_PENDING_VISUAL_CONTEXT_TTL_SECONDS,
            limit=MAX_MERGED_PRIVATE_IMAGES,
        )
        if not events:
            return None

        latest_metadata = (
            events[-1].get("metadata")
            if isinstance(events[-1].get("metadata"), dict)
            else {}
        )
        latest_paths = self._normalize_visual_image_paths(latest_metadata.get("image_paths"))
        if not any(os.path.isfile(path) for path in latest_paths):
            return None

        batch = [events[-1]]
        latest_notes = self._normalize_visual_note_slots(
            latest_paths,
            latest_metadata.get("visual_notes"),
        )
        next_received_at = float(events[-1].get("received_at", 0.0) or 0.0)
        for event in reversed(events[:-1]):
            received_at = float(event.get("received_at", 0.0) or 0.0)
            if next_received_at - received_at > GROUP_VISUAL_BATCH_GAP_SECONDS:
                break
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            event_paths = self._normalize_visual_image_paths(metadata.get("image_paths"))
            event_notes = self._normalize_visual_note_slots(event_paths, metadata.get("visual_notes"))
            if not any(latest_notes) and any(event_notes):
                break
            batch.append(event)
            next_received_at = received_at
        batch.reverse()

        image_paths = []
        image_senders = []
        visual_notes = []
        for event in batch:
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            event_paths = self._normalize_visual_image_paths(metadata.get("image_paths"))
            event_notes = self._normalize_visual_note_slots(event_paths, metadata.get("visual_notes"))
            for index, path in enumerate(event_paths):
                if not os.path.isfile(path):
                    continue
                image_paths.append(path)
                image_senders.append(str(event.get("sender", "") or "").strip())
                visual_notes.append(event_notes[index])
        if not image_paths:
            return None
        return self._set_pending_visual_context(
            chat_name,
            image_paths,
            chat_type="group",
            senders=image_senders,
            visual_notes=visual_notes,
            observed_at=float(batch[-1].get("received_at", now) or now),
        )

    def _get_pending_visual_context(self, chat_name, *, chat_type):
        self._ensure_message_runtime_state()
        key = self._visual_context_key(chat_name, chat_type)
        with self._chat_merge_lock:
            context = dict(self._pending_visual_contexts.get(key) or {})
            if not context:
                context = None
        if context is None and key[0] == "group":
            context = self._restore_group_pending_visual_context(chat_name)
        if not context:
            return None
        with self._chat_merge_lock:
            if float(context.get("expires_at", 0) or 0) < time.time():
                self._pending_visual_contexts.pop(key, None)
                return None
            context["image_paths"] = self._normalize_visual_image_paths(context.get("image_paths"))
            if not context["image_paths"]:
                self._pending_visual_contexts.pop(key, None)
                return None
            context["image_senders"] = self._normalize_visual_sender_slots(
                context["image_paths"],
                context.get("image_senders"),
            )
            context["visual_notes"] = self._normalize_visual_note_slots(
                context["image_paths"],
                context.get("visual_notes"),
            )
            return context

    def _clear_pending_visual_context(self, chat_name, *, chat_type):
        self._ensure_message_runtime_state()
        key = self._visual_context_key(chat_name, chat_type)
        with self._chat_merge_lock:
            self._pending_visual_contexts.pop(key, None)

    def _pending_visual_context_ready_to_clear(self, chat_name, *, chat_type):
        context = self._get_pending_visual_context(chat_name, chat_type=chat_type)
        if not context:
            return True
        image_paths = self._normalize_visual_image_paths(context.get("image_paths"))
        visual_notes = self._normalize_visual_note_slots(image_paths, context.get("visual_notes"))
        return bool(visual_notes and any(visual_notes))

    def _remember_visual_notes(self, chat_name, image_paths, visual_notes, *, chat_type):
        normalized_paths = self._normalize_visual_image_paths(image_paths)
        normalized_notes = self._normalize_visual_note_slots(normalized_paths, visual_notes)
        if not normalized_paths or not any(normalized_notes):
            return False
        updated = False
        memory_manager = getattr(self, "memory_manager", None)
        attach_notes = getattr(memory_manager, "attach_visual_notes", None)
        if callable(attach_notes):
            try:
                updated = bool(attach_notes(
                    chat_name,
                    normalized_paths,
                    normalized_notes,
                    chat_type=chat_type,
                )) or updated
            except Exception as exc:
                log(level="WARNING", message=f"回写图片摘要到聊天记录失败: {exc}")
        self._ensure_message_runtime_state()
        key = self._visual_context_key(chat_name, chat_type)
        with self._chat_merge_lock:
            context = dict(getattr(self, "_pending_visual_contexts", {}).get(key) or {})
            context_paths = self._normalize_visual_image_paths(context.get("image_paths"))
            if context_paths and context_paths == normalized_paths:
                existing_notes = self._normalize_visual_note_slots(context_paths, context.get("visual_notes"))
                if existing_notes != normalized_notes:
                    context["visual_notes"] = list(normalized_notes)
                    self._pending_visual_contexts[key] = context
                    updated = True
        return updated

    def _api_error_reply_parts(self):
        reply = str(getattr(self.config, 'api_error_reply', '') or '').strip()
        if reply:
            return [reply]
        log(level="WARNING", message="AI 回复失败，未配置失败固定回复，本次未发送回复")
        return []

    def _reply_preprocess_max_chars(self):
        try:
            return max(1, min(10000, int(getattr(self.config, "reply_preprocess_max_chars", 100) or 100)))
        except (TypeError, ValueError):
            return 100

    def _reply_preprocess_fallback_policy(self):
        fallback = str(getattr(self.config, "reply_preprocess_fallback_reply", "") or "").strip()
        return {
            "fallback_reply": fallback,
            "reply_once": bool(getattr(self.config, "reply_preprocess_fallback_once", False)),
        }

    def _reply_rewrite_prompt(self):
        return (
            "你刚才的输出不适合直接通过微信发送，请只输出最终的回复消息，"
            f"控制在 {self._reply_preprocess_max_chars()} 字以内。\n"
            "不要解释，不要输出内部字段。"
        )

    def _reply_preprocess_candidate(self, reply):
        raw = str(reply or "")
        cleaned = clean_ai_reply_text(raw)
        if raw.strip() and not cleaned.strip():
            return ""
        if getattr(self.config, "clean_ai_reply_switch", False):
            return cleaned.strip()
        return raw.strip()

    def _preprocess_ai_reply_for_send(self, reply, *, rewrite_func, user_key="", scene_label="AI回复"):
        max_chars = self._reply_preprocess_max_chars()
        candidate = self._reply_preprocess_candidate(reply)
        allowed, reason = evaluate_reply_preprocess_admission(candidate, max_chars=max_chars)
        if allowed:
            return {"status": "ok", "reply": candidate, "mark_fallback": False}

        detail = describe_reply_preprocess_rejection(candidate, max_chars=max_chars)
        reason_label = reply_preprocess_rejection_label(reason)
        log(level="WARNING", message=f"{scene_label}：回复预处理拦截，原因：{reason_label}，详情：{detail or '-'}，动作：准备重写一次")
        try:
            rewritten = rewrite_func(self._reply_rewrite_prompt())
        except Exception as exc:
            log(level="ERROR", message=f"{scene_label}：回复预处理重写接口调用失败：{exc}")
            return {"status": "api_error", "reply": ""}

        if is_api_error_reply(rewritten):
            log(level="ERROR", message=f"{scene_label}：回复预处理重写返回接口错误")
            return {"status": "api_error", "reply": ""}

        candidate = self._reply_preprocess_candidate(rewritten)
        allowed, retry_reason = evaluate_reply_preprocess_admission(candidate, max_chars=max_chars)
        if allowed:
            log(message=f"{scene_label}：回复预处理重写成功")
            return {"status": "ok", "reply": candidate, "mark_fallback": False}

        retry_detail = describe_reply_preprocess_rejection(candidate, max_chars=max_chars)
        retry_reason_label = reply_preprocess_rejection_label(retry_reason)
        log(level="WARNING", message=f"{scene_label}：回复预处理重写后仍不合格，原因：{retry_reason_label}，详情：{retry_detail or '-'}")
        policy = self._reply_preprocess_fallback_policy()
        fallback_reply = policy["fallback_reply"]
        if not fallback_reply:
            log(level="WARNING", message=f"{scene_label}：未配置异常兜底回复，本次静默")
            return {"status": "silent", "reply": "", "mark_fallback": False}
        if policy["reply_once"] and user_key:
            user_data = self._reply_once_user_data(user_key)
            if user_data.get("preprocess_fallback_notified"):
                return {"status": "skip", "reply": "", "mark_fallback": False}
        return {
            "status": "fallback",
            "reply": fallback_reply,
            "mark_fallback": bool(policy["reply_once"] and user_key),
        }

    def _voice_reply_state_path(self):
        data_dir = str(getattr(getattr(self, "config", None), "DATA_DIR", getattr(self, "DATA_DIR", "")) or "").strip()
        if not data_dir:
            _base = os.path.dirname(sys.executable) if hasattr(sys, '_MEIPASS') else os.path.abspath(".")
            data_dir = os.path.join(_base, 'data')
        return os.path.join(data_dir, "config", "voice_reply_state.json")

    def _save_voice_reply_state(self):
        state = getattr(self, "_voice_reply_state", None)
        if state is None:
            return
        save_voice_reply_state(self._voice_reply_state_path(), state)

    def _private_voice_context_text(self, message):
        msg_type = str(getattr(message, "type", "") or "").strip().lower()
        if msg_type == "image":
            return ""
        raw = str(getattr(message, "content", "") or "").strip()
        if not raw:
            return ""
        if QUOTE_IMAGE_MARKER in raw:
            raw, _image_paths = split_quoted_image_message(raw)
        return build_tts_context_text(raw)

    def _group_voice_context_text(self, message, content_without_at):
        if str(getattr(message, "type", "") or "").strip().lower() == "image":
            return ""
        raw = str(content_without_at or "").strip()
        if not raw:
            return ""
        if QUOTE_IMAGE_MARKER in raw:
            raw, _image_paths = split_quoted_image_message(raw)
        return build_tts_context_text(raw)

    def _active_tts_config(self, conversation_name="", *, chat_type="private"):
        tts_index = getattr(self.config, 'tts_index', 0)
        tts_map = {}
        if chat_type == "group":
            tts_map = getattr(self.config, 'group_tts_map', {}) or {}
        elif conversation_name and self._is_private_whitelist_user(conversation_name):
            tts_map = getattr(self.config, 'chat_tts_map', {}) or {}
        if isinstance(tts_map, dict) and conversation_name in tts_map:
            try:
                tts_index = int(tts_map.get(conversation_name))
            except (TypeError, ValueError):
                tts_index = getattr(self.config, 'tts_index', 0)
        return select_tts_config(
            getattr(self.config, 'tts_configs', []) or [],
            tts_index,
        )

    @staticmethod
    def _format_voice_reply_error(exc):
        message = str(exc)
        if "ffmpeg" not in message.lower():
            return message
        return (
            f"{message} "
            "当前 wxautox4 语音发送除了虚拟麦克风外，还需要 ffmpeg/ffprobe 可直接执行；"
            "请确认已安装并加入 PATH，同时按 wxauto 文档把 Windows 输入设备切到 CABLE Output。"
        )

    def _should_log_voice_reply_limit_warning(self, state_key, limiter, *, now, limit_count, limit_hours):
        warning_keys = getattr(self, "_voice_reply_limit_warning_keys", None)
        if warning_keys is None:
            warning_keys = set()
            self._voice_reply_limit_warning_keys = warning_keys
        if len(warning_keys) > 1000:
            warning_keys.clear()

        state = getattr(limiter, "state", None)
        item = getattr(state, "limits", {}).get(state_key, {}) if state is not None else {}
        window_started_at = str(item.get("window_started_at", "") or "")
        if not window_started_at:
            window_started_at = now.isoformat(timespec="seconds")
        key = (str(state_key or ""), str(limit_count or ""), str(limit_hours or ""), window_started_at)
        if key in warning_keys:
            return False
        warning_keys.add(key)
        return True

    @staticmethod
    def _format_reply_log_item(content, *, kind="text"):
        text = " ".join(str(content or "").splitlines()).strip()
        if not text:
            return ""
        type_label = {
            "voice": "语音",
            "audio": "语音",
            "quote": "引用",
            "image": "图片",
            "file": "文件",
        }.get(str(kind or "text").strip().lower(), "")
        return f"[{type_label}]{text}" if type_label else text

    @staticmethod
    def _log_reply_contents(scene, chat_name, items):
        reply_items = [str(item or "").strip() for item in (items or []) if str(item or "").strip()]
        if not reply_items:
            return
        count_text = f"（{len(reply_items)}条）" if len(reply_items) > 1 else ""
        log(
            level="INFO",
            message=f"{scene} {chat_name}：本轮回复{count_text}：{' ｜ '.join(reply_items)}",
        )

    def _reply_actions_from_text(self, parts, *, source=ReplySource.AI):
        actions = []
        for part in parts or []:
            text = str(part or "").strip()
            if not text:
                continue
            segments = list(self.config.split_long_text(text)) if len(text) >= LONG_REPLY_SEGMENT_CHARS else [text]
            actions.extend(ReplyAction(ReplyKind.TEXT, segment, source) for segment in segments if str(segment or "").strip())
        return tuple(actions)

    def _reply_turn(self, chat, message, actions, *, chat_type="private"):
        turn_id = self._ensure_reply_job(chat, message, chat_type=chat_type)
        if not turn_id:
            return None
        return ReplyTurn(
            turn_id=turn_id,
            conversation=str(getattr(chat, "who", "") or "").strip(),
            expected_version=int(getattr(message, "_wxbot_event_version", 0) or 0),
            expires_at=float(getattr(message, "_wxbot_reply_expires_at", 0.0) or 0.0),
            event_ids=self._reply_event_ids(message),
            actions=tuple(actions),
            chat_type=chat_type,
        )

    def _prepare_reply_delivery(self, turn, action, action_id, context):
        if not isinstance(context, dict) or context.get("chat") is None:
            raise RuntimeError("reply delivery context is missing")
        if self.is_stop_requested():
            return False
        delayed = context.setdefault("delayed_action_ids", set())
        index = int(str(action_id).rsplit(":", 1)[-1])
        if index > 0 and action_id not in delayed:
            split_enabled = bool(
                getattr(self.config, "group_split_reply_switch", False)
                if turn.chat_type == "group"
                else getattr(self.config, "chat_split_reply_switch", False)
            )
            delay_enabled = bool(
                getattr(self.config, "group_split_reply_delay_switch", True)
                if turn.chat_type == "group"
                else getattr(self.config, "chat_split_reply_delay_switch", True)
            )
            self._human_delay_for_reply_part(
                part_text=action.content,
                split_continuation=True,
                is_last=index == len(turn.actions) - 1,
                delay_enabled=split_enabled and delay_enabled,
            )
            delayed.add(action_id)
        return not self.is_stop_requested()

    def _send_reply_delivery(self, turn, action, action_id, context):
        if not isinstance(context, dict):
            raise RuntimeError("reply delivery context is missing")
        chat = context["chat"]
        index = int(str(action_id).rsplit(":", 1)[-1])
        at = str(context.get("at_first", "") or "") if index == 0 else ""
        tracker = getattr(self, "_reply_echo_tracker", None)
        if tracker is not None:
            tracker.reserve(
                action_id,
                turn.conversation,
                action,
                chat_type=turn.chat_type,
                at=at,
            )
        conversation_version = turn.expected_version
        try:
            if action.kind == ReplyKind.QUOTE:
                try:
                    result = self._ui_quote_message(
                        chat,
                        context.get("message"),
                        action.content,
                        at=at,
                        journal=False,
                        conversation_version=conversation_version,
                        echo_delivery_ids=(action_id,),
                        expires_at=turn.expires_at,
                    )
                except MessageLocateError as exc:
                    log(level="WARNING", message=f"引用原消息失败，已降级普通文本：{exc}")
                    result = chat.SendMsg(
                        msg=action.content,
                        at=at or None,
                        conversation_version=conversation_version,
                        echo_delivery_ids=(action_id,),
                        expires_at=turn.expires_at,
                    )
            elif action.kind == ReplyKind.VOICE:
                result = chat.SendAudio(
                    filepath=action.send_value,
                    duration=None,
                    conversation_version=conversation_version,
                    journal=False,
                    echo_delivery_ids=(action_id,),
                    expires_at=turn.expires_at,
                )
            elif action.kind == ReplyKind.FILE:
                result = chat.SendFiles(
                    filepath=action.send_value or action.content,
                    conversation_version=conversation_version,
                    journal=False,
                    echo_delivery_ids=(action_id,),
                    expires_at=turn.expires_at,
                )
            else:
                result = chat.SendMsg(
                    msg=action.content,
                    at=at or None,
                    conversation_version=conversation_version,
                    echo_delivery_ids=(action_id,),
                    expires_at=turn.expires_at,
                )
        except wechat_ui_actions.IntentCancelled as exc:
            if tracker is not None:
                tracker.discard(action_id)
            current_version = self._message_store.conversation_version(
                turn.conversation,
                chat_type=turn.chat_type,
            )
            status = (
                DeliveryStatus.STALE
                if current_version != turn.expected_version
                else DeliveryStatus.EXPIRED
                if time.time() >= turn.expires_at
                else DeliveryStatus.CANCELLED
            )
            raise DeliveryNotStarted(status, str(exc)) from exc
        return ReplyCountStore.was_send_success(result)

    @staticmethod
    def _delivery_result_is_handled(delivery):
        return bool(
            delivery
            and delivery.status in {
                DeliveryStatus.DONE,
                DeliveryStatus.STALE,
                DeliveryStatus.CANCELLED,
                DeliveryStatus.EXPIRED,
            }
        )

    def _deliver_reply_actions(
        self,
        chat,
        message,
        actions,
        *,
        chat_type="private",
        at_first="",
        sent_items=None,
    ):
        actions = tuple(actions or ())
        if not actions:
            self._cancel_unfinished_reply_job(message, "reply contains no deliverable actions")
            return None
        if self.is_stop_requested():
            self._cancel_failed_reply_attempt(message, "robot stopped before reply delivery")
            return DeliveryResult(DeliveryStatus.CANCELLED)
        coordinator = getattr(self, "_reply_delivery_coordinator", None)
        turn = self._reply_turn(chat, message, actions, chat_type=chat_type)
        if coordinator is None or turn is None:
            return None
        context = {
            "chat": chat,
            "message": message,
            "at_first": str(at_first or ""),
            "delayed_action_ids": set(),
        }
        result = None
        retry_delay = 0.0
        retry_budget = max(0.0, turn.expires_at - time.time())
        while True:
            if retry_delay:
                wait_for = min(retry_delay, retry_budget)
                if wait_for > 0 and not self.is_stop_requested():
                    self._wait_or_stop_requested(wait_for)
                retry_budget = max(0.0, retry_budget - wait_for)
            result = coordinator.deliver(turn, context)
            if result.status not in {DeliveryStatus.RETRY, DeliveryStatus.BLOCKED}:
                break
            if self.is_stop_requested():
                coordinator.cancel(turn.turn_id, "robot stopped before reply target recovered")
                break
            if retry_budget <= 0 or time.time() >= turn.expires_at:
                result = coordinator.deliver(turn, context)
                break
            retry_delay = 30.0 if retry_delay == 0 else 60.0
        if result is None or result.status in {DeliveryStatus.RETRY, DeliveryStatus.BLOCKED}:
            coordinator.cancel(turn.turn_id, "reply target did not recover before reply TTL")
            return result
        if isinstance(sent_items, list) and result.completed:
            for action in turn.actions[: result.completed]:
                sent_items.append(self._format_reply_log_item(action.content, kind=action.kind.value))
        return result

    def _try_send_voice_reply(
        self,
        chat,
        clean_reply,
        *,
        state_key,
        limit_count,
        limit_hours,
        context_text="",
        section_id="",
        expected_sequence=None,
        message=None,
    ):
        tts_text = normalize_text_for_tts(clean_reply)
        if not is_text_suitable_for_voice(tts_text, max_chars=100):
            return False
        if str(state_key or "").startswith("private:") and not self._private_reply_can_continue(
            chat,
            expected_sequence=expected_sequence,
        ):
            return False
        now = datetime.now()
        limiter = VoiceReplyLimiter(getattr(self, "_voice_reply_state", None) or load_voice_reply_state(self._voice_reply_state_path()))
        self._voice_reply_state = limiter.state
        if not limiter.can_send(
            state_key,
            now=now,
            limit_count=limit_count,
            limit_hours=limit_hours,
        ):
            if self._should_log_voice_reply_limit_warning(
                state_key,
                limiter,
                now=now,
                limit_count=limit_count,
                limit_hours=limit_hours,
            ):
                log(
                    level="WARNING",
                    message=(
                        f"{getattr(chat, 'who', '')} 语音回复触发上限，"
                        f"当前 {limit_hours} 小时最多 {limit_count} 条，已降级文字回复"
                    ),
                )
            return False
        audio_path = ""
        try:
            chat_type = "private" if str(state_key or "").startswith("private:") else "group"
            tts_cfg = self._active_tts_config(
                getattr(chat, "who", ""),
                chat_type=chat_type,
            )
            if not tts_cfg:
                return False
            data_dir = str(getattr(self.config, "DATA_DIR", getattr(self, "DATA_DIR", "")) or "").strip()
            if not data_dir:
                data_dir = os.path.join(os.path.abspath("."), "data")
            audio_path = make_tts_cache_path(os.path.join(data_dir, 'cache', 'tts'), suffix='mp3')
            synth_cfg = dict(tts_cfg)
            if str(context_text or "").strip():
                synth_cfg["context_text"] = str(context_text or "").strip()
            if str(section_id or "").strip():
                synth_cfg["section_id"] = str(section_id or "").strip()
            self._record_tts_api_request()
            create_tts_client(synth_cfg).synthesize(tts_text, audio_path)
            if str(state_key or "").startswith("private:") and not self._private_reply_can_continue(
                chat,
                expected_sequence=expected_sequence,
            ):
                return False
            if message is not None:
                action = ReplyAction(
                    ReplyKind.VOICE,
                    clean_reply,
                    ReplySource.AI,
                    send_value=str(audio_path),
                )
                delivery = self._deliver_reply_actions(
                    chat,
                    message,
                    (action,),
                    chat_type=chat_type,
                )
                if delivery is not None:
                    if delivery.status == DeliveryStatus.UNCERTAIN:
                        log(level="ERROR", message="语音回复已提交但结果未知，已禁止自动降级文字")
                    if delivery.completed:
                        limiter.mark_sent(state_key, now=now, limit_hours=limit_hours)
                        return True
                    raise _ReplyTurnAborted(delivery.status)
            return False
        except _ReplyTurnAborted:
            raise
        except Exception as exc:
            log(
                level="WARNING",
                message=f"语音回复失败，已降级文字发送：{self._format_voice_reply_error(exc)}",
            )
        finally:
            self._remove_temp_audio_file(audio_path)
        return False

    def _send_private_ai_reply_parts(
        self,
        chat,
        parts,
        *,
        message=None,
        expected_sequence=None,
        sent_items=None,
    ):
        if not self._private_reply_can_continue(chat, expected_sequence=expected_sequence):
            return False, True
        actions = self._reply_actions_from_text(parts)
        if not actions:
            return False, False
        if message is not None:
            delivery = self._deliver_reply_actions(
                chat,
                message,
                actions,
                chat_type="private",
                sent_items=sent_items,
            )
            if delivery is not None:
                return delivery.completed > 0, self._delivery_result_is_handled(delivery)
        return False, False

    def _send_keyword_reply_actions(
        self,
        chat,
        actions,
        *,
        at=None,
        message=None,
    ):
        planned = []
        for item in actions or []:
            item = dict(item or {})
            if str(item.get("type") or "").strip().lower() == "text":
                content = str(item.get("content") or "").strip()
                if content:
                    planned.append(ReplyAction(ReplyKind.TEXT, content, ReplySource.KEYWORD))
                continue
            path = str(item.get("path") or "").strip()
            if not path or not os.path.isfile(path):
                log(level="ERROR", message=f"关键词回复文件不存在，已跳过：{path}")
                continue
            planned.append(ReplyAction(
                ReplyKind.FILE,
                f"[文件] {os.path.basename(path)}".strip(),
                ReplySource.KEYWORD,
                send_value=path,
            ))
        if not planned:
            log(level="ERROR", message=f"关键词回复命中但没有可发送内容，已停止处理：{chat.who}")
            return False, False

        is_group = getattr(chat, "chat_type", "") == "group" or chat.who in getattr(self.config, "group", [])
        if not is_group and not self._private_reply_can_continue(chat):
            return False, True
        if message is not None:
            delivery = self._deliver_reply_actions(
                chat,
                message,
                planned,
                chat_type="group" if is_group else "private",
                at_first=at,
            )
            if delivery is not None:
                success = delivery.completed > 0
                if success:
                    log(level="INFO", message=f"关键词回复成功：{chat.who}，发送 {delivery.completed} 条")
                return success, self._delivery_result_is_handled(delivery)
        return False, False

    def _get_reply_count_key(self, chat, message=None):
        """获取回复计数器 key；当前 wxautox4 可用稳定字段有限，先集中使用 chat.who。"""
        return str(getattr(chat, 'who', '') or '').strip()

    def _get_group_reply_once_key(self, chat, message=None):
        group_name = str(getattr(chat, 'who', '') or '').strip()
        sender = str(getattr(message, 'sender', '') or '').strip()
        if not group_name:
            return ""
        return f"group:{group_name}:{sender}" if sender else f"group:{group_name}"

    def _text_reply_limit_settings(self, chat_type):
        scope = "group" if chat_type == "group" else "chat"
        return {
            "switch": bool(getattr(self.config, f"{scope}_text_reply_limit_switch", False)),
            "count": getattr(self.config, f"{scope}_text_reply_limit_count", 50),
            "hours": getattr(self.config, f"{scope}_text_reply_limit_hours", 5),
            "ai_reply": bool(getattr(self.config, f"{scope}_text_reply_limit_ai_reply", True)),
            "reply": str(getattr(self.config, f"{scope}_text_reply_limit_reply", "") or "").strip(),
            "reply_once": bool(getattr(self.config, f"{scope}_text_reply_limit_reply_once", False)),
        }

    def _memory_context_raw_limit(self, message_limit):
        try:
            max_count = int(getattr(self.config, "memory_max_count", 5000) or 5000)
        except Exception:
            max_count = 5000
        try:
            message_limit = int(message_limit)
        except Exception:
            message_limit = 0
        return max(max_count, message_limit, 0)

    def _load_chat_history(self, chat_who, raw_limit, *, chat_type, event_ids=()):
        store = getattr(self, "_message_store", None)
        if store is not None:
            events = store.history(
                chat_who,
                raw_limit,
                chat_type=chat_type,
                before_event_ids=event_ids,
            )
            return [MemoryManager._history_message(event) for event in events]
        if self.memory_manager:
            return self.memory_manager.get_messages(
                chat_who,
                raw_limit,
                chat_type=chat_type,
            ) or []
        return []

    def _get_model_context_history(
        self,
        chat_who,
        *,
        event_ids=(),
        chat_type="private",
        extra_messages=(),
    ):
        try:
            count = max(0, int(getattr(self.config, 'memory_context_count', 0) or 0))
        except Exception:
            count = 0
        if count <= 0:
            return []
        try:
            raw_limit = self._memory_context_raw_limit(count)
            raw_history = self._load_chat_history(
                chat_who,
                raw_limit,
                chat_type=chat_type,
                event_ids=event_ids,
            )
            raw_history.extend(list(extra_messages or ()))
            history = build_model_visible_history(
                raw_history,
                message_limit=count,
            )
            return history
        except Exception as e:
            log(level="WARNING", message=f"读取AI上下文失败: {e}")
            return []

    def _text_reply_limit_history(self, chat_who, *, event_ids=(), chat_type="private"):
        try:
            count = max(0, int(getattr(self.config, 'memory_context_count', 0) or 0))
        except Exception:
            count = 0
        if count <= 0:
            return []
        try:
            raw_limit = self._memory_context_raw_limit(count)
            raw_history = self._load_chat_history(
                chat_who,
                raw_limit,
                chat_type=chat_type,
                event_ids=event_ids,
            )
            history = build_model_visible_history(
                raw_history,
                message_limit=count,
            )
            return history
        except Exception as e:
            log(level="WARNING", message=f"读取轮数超限结束语上下文失败: {e}")
            return []

    def _build_text_reply_limit_ai_prompt(self, chat_name, *, chat_type="private"):
        system = getattr(self, "prompt_system", None)
        if system is None:
            system = self._init_prompt_system()
        return system.render_template_prompt(
            CLOSING_REPLY_PROMPT_FILE,
            chat_name,
            chat_type=chat_type,
        )

    def _generate_text_reply_limit_reply(self, chat, message, *, chat_type="private"):
        prompt = self._build_text_reply_limit_ai_prompt(chat.who, chat_type=chat_type)
        history = self._text_reply_limit_history(
            chat.who,
            event_ids=self._reply_event_ids(message),
            chat_type=chat_type,
        )
        content = str(getattr(message, 'content', '') or '').strip()
        if chat_type == "group":
            sender = str(getattr(message, "sender", "") or "").strip()
            if sender:
                content = f"{sender}: {content}"
            api = self._get_group_api(chat.who)
        else:
            api = self._get_chat_api(chat.who)
        reply = api.chat(content, prompt=prompt, history=history)
        return str(reply or '').strip()

    def _send_text_reply_limit_parts(self, chat, message, parts, *, chat_type, sent_items=None):
        if chat_type != "group":
            return self._send_private_ai_reply_parts(chat, parts, message=message, sent_items=sent_items)
        actions = list(self._reply_actions_from_text(parts))
        quote_first = bool(getattr(self.config, "group_reply_quote", False))
        if quote_first and actions:
            first = actions[0]
            actions[0] = ReplyAction(ReplyKind.QUOTE, first.content, first.source)
        sender = str(getattr(message, "sender", "") or "").strip()
        delivery = self._deliver_reply_actions(
            chat,
            message,
            actions,
            chat_type="group",
            at_first=(
                sender
                if bool(getattr(self.config, "group_reply_at_msg", False))
                else ""
            ),
            sent_items=sent_items,
        )
        if delivery is None:
            return False, False
        return delivery.completed > 0, self._delivery_result_is_handled(delivery)

    def _should_log_text_reply_limit_warning(self, user_key, user_data, *, limit_count, limit_hours):
        warning_keys = getattr(self, "_text_reply_limit_warning_keys", None)
        if warning_keys is None:
            warning_keys = set()
            self._text_reply_limit_warning_keys = warning_keys
        if len(warning_keys) > 1000:
            warning_keys.clear()

        window_started_at = str((user_data or {}).get("window_started_at", "") or "")
        key = (str(user_key or ""), str(limit_count or ""), str(limit_hours or ""), window_started_at)
        if key in warning_keys:
            return False
        warning_keys.add(key)
        return True

    def _check_text_reply_limit(self, chat, user_key, message=None, *, chat_type="private"):
        """检查并处理当前会话对象的回复次数限制；返回 (是否已处理, 发送结果)。"""
        settings = self._text_reply_limit_settings(chat_type)
        max_round = settings["count"]
        limit_hours = settings["hours"]
        if not self._text_reply_limit_reached(user_key, chat_type=chat_type):
            return False, True
        user_data = self.reply_count_store.get_user(user_key, limit_hours=limit_hours)
        if settings["reply_once"] and user_data.get("limit_notified"):
            return True, True
        scene_label = "群聊" if chat_type == "group" else "私聊"
        target_label = str(getattr(chat, "who", "") or "")
        if chat_type == "group":
            sender = str(getattr(message, "sender", "") or "").strip()
            if sender:
                target_label = f"{target_label} / {sender}"
        if self._should_log_text_reply_limit_warning(
            user_key,
            user_data,
            limit_count=max_round,
            limit_hours=limit_hours,
        ):
            log(
                level="WARNING",
                message=f"{scene_label} {target_label} 触发回复上限：{limit_hours} 小时最多 {max_round} 轮",
            )
        reply_text = ""
        if settings["ai_reply"]:
            try:
                log(message=f"{scene_label} {target_label} 触发轮数超限，使用 AI 自动生成结束语")
                reply_text = self._generate_text_reply_limit_reply(chat, message, chat_type=chat_type)
                if is_api_error_reply(reply_text):
                    log(level="WARNING", message="轮数超限结束语生成遇到 API 错误，转入接口报错回复策略")
                    if getattr(self.config, "api_error_reply_once", False):
                        api_user_data = self._reply_once_user_data(user_key)
                        if api_user_data.get("api_err_notified"):
                            return True, True
                    parts = self._api_error_reply_parts()
                    sent_reply_items = []
                    send_success, result = self._send_text_reply_limit_parts(
                        chat,
                        message,
                        parts,
                        chat_type=chat_type,
                        sent_items=sent_reply_items,
                    )
                    if send_success:
                        self._record_reply_metric_success(chat.who, chat_type=chat_type)
                        self._log_reply_contents(scene_label, chat.who, sent_reply_items)
                        if getattr(self.config, "api_error_reply_once", False):
                            self.reply_count_store.mark_api_err_notified(user_key)
                    return True, result
            except Exception as e:
                log(level="WARNING", message=f"轮数超限结束语生成失败，已静默跳过: {e}")
                reply_text = ""
        else:
            reply_text = settings["reply"]
        if not reply_text:
            return True, True

        sent_reply_items = []
        send_success, result = self._send_text_reply_limit_parts(
            chat,
            message,
            [reply_text],
            chat_type=chat_type,
            sent_items=sent_reply_items,
        )
        if send_success:
            self._record_reply_metric_success(chat.who, chat_type=chat_type)
            self._log_reply_contents(scene_label, chat.who, sent_reply_items)
            if settings["reply_once"]:
                self.reply_count_store.mark_limit_notified(user_key)
        return True, result

    def _text_reply_limit_reached(self, user_key, *, chat_type="private"):
        settings = self._text_reply_limit_settings(chat_type)
        return bool(
            settings["switch"]
            and user_key
            and not self.reply_count_store.can_consume(
                user_key,
                limit_count=settings["count"],
                limit_hours=settings["hours"],
            )
        )

    def _check_text_reply_limit_runtime(self, chat, user_key, message=None, *, chat_type="private"):
        return self._check_text_reply_limit(chat, user_key, message=message, chat_type=chat_type)


    def _material_source_runtime_enabled(self):
        sources = [
            str(item or "").strip()
            for item in (getattr(self.config, "material_source_list", []) or [])
            if str(item or "").strip()
        ]
        return bool(sources)

    def _is_material_source_chat(self, chat):
        conversation = ConversationRef.from_wx_chat(chat)
        return bool(
            self._material_source_runtime_enabled()
            and is_material_source(
                getattr(self.config, 'material_source_list', []),
                conversation.who,
            )
            and conversation.chat_type
            == self._material_source_chat_type(conversation.who)
        )

    def _current_ai_material_outreach_config(self):
        raw = {}
        if isinstance(getattr(self.config, "config", None), dict):
            raw.update(self.config.config)
        for field, default in (
            ("ai_material_outreach_switch", False),
            ("ai_material_outreach_daily_limit_per_friend", 3),
            ("ai_material_outreach_delay_min_seconds", 10),
            ("ai_material_outreach_delay_max_seconds", 30),
            ("ai_material_outreach_detection_interval_minutes", 30),
            ("ai_material_outreach_detection_message_threshold", 30),
        ):
            raw.setdefault(field, getattr(self.config, field, default))
        return normalize_ai_auto_outreach_runtime_config(raw)

    def _get_default_chat_api(self):
        index = self._get_active_default_chat_api_index()
        api = self._get_api_instance_by_index(index)
        return self._wrap_chat_api_for_failover(api, index=index, tracked_default=True)

    def _load_ai_detection_state(self):
        runtime = self._load_material_outreach_runtime()
        return normalize_ai_detection_state(runtime.get("ai_detection_state"))

    def _mutate_ai_detection_state(self, mutator):
        runtime = self._material_outreach_store().mutate_runtime(
            lambda payload: self._mutate_runtime_ai_detection_state_payload(payload, mutator)
        )
        return normalize_ai_detection_state(runtime.get("ai_detection_state"))

    def _mutate_runtime_ai_detection_state_payload(self, payload, mutator):
        payload = dict(payload) if isinstance(payload, dict) else {}
        current_state = normalize_ai_detection_state(payload.get("ai_detection_state"))
        next_state = mutator(dict(current_state))
        payload["ai_detection_state"] = normalize_ai_detection_state(next_state)
        return payload

    def _save_ai_detection_state(self, state):
        return self._mutate_ai_detection_state(lambda _current_state: state)

    def _clear_ai_detection_target(self, target):
        target = str(target or "").strip()
        if not target:
            return self._load_ai_detection_state()
        return self._mutate_ai_detection_state(
            lambda current_state: clear_ai_detection_target(target, current_state)
        )

    def _clear_ai_detection_target_if_snapshot(self, target, expected_record):
        target = str(target or "").strip()
        if not target:
            return self._load_ai_detection_state()
        expected_record = normalize_ai_detection_record(expected_record)
        return self._mutate_ai_detection_state(
            lambda current_state: clear_ai_detection_target_if_matches(
                target,
                current_state,
                expected_record,
            )
        )

    def _ai_outreach_daily_limit_reached(self, target, *, now=None):
        target = str(target or "").strip()
        if not target:
            return False
        now = now or datetime.now()
        config = self._current_ai_material_outreach_config()
        daily_limit = max(0, int(config.get("ai_material_outreach_daily_limit_per_friend") or 0))
        if daily_limit <= 0:
            return False
        success_count = 0
        for record in self._load_material_send_records() or []:
            if record.get("task_id") != AI_AUTO_OUTREACH_TASK_ID:
                continue
            if str(record.get("target") or "").strip() != target or not record.get("success"):
                continue
            try:
                sent_at = datetime.fromisoformat(str(record.get("sent_at") or "").strip())
            except ValueError:
                continue
            if sent_at.date() == now.date():
                success_count += 1
        return success_count >= daily_limit

    def _ai_outreach_queue_result(self, *, evaluation_attempted=False, queued=False, queue_id=""):
        return {
            "evaluation_attempted": bool(evaluation_attempted),
            "queued": bool(queued),
            "queue_id": str(queue_id or "").strip(),
        }

    def _run_ai_material_outreach_evaluation(self, target, *, message_text, history, source):
        target = str(target or "").strip()
        if not target:
            return self._ai_outreach_queue_result()
        config = self._current_ai_material_outreach_config()
        if not config.get("ai_material_outreach_switch"):
            return self._ai_outreach_queue_result()
        materials = self._load_material_outreach_materials()
        candidate_cards = filter_ai_outreach_candidate_pool(
            build_ai_candidate_material_cards(materials),
            allowed_sources=config.get("ai_material_outreach_allowed_sources"),
        )
        send_records = self._load_material_send_records()
        target_cards = build_ai_outreach_candidates_for_target(candidate_cards, send_records, target)
        if not target_cards:
            return self._ai_outreach_queue_result()
        with self._material_outreach_runtime_lock():
            queue_records = self._load_ai_pending_queue()
            latest_send_records = self._load_material_send_records()
            target_cards = build_ai_outreach_candidates_for_target(candidate_cards, latest_send_records, target)
            gate = evaluate_ai_outreach_gate(
                config,
                is_private_ai_reply=True,
                target=target,
                candidate_cards=target_cards,
                send_records=latest_send_records,
                queue_records=queue_records,
            )
        if not gate.get("allowed"):
            return self._ai_outreach_queue_result()
        prompt = self._build_ai_outreach_decision_prompt(
            target_cards,
            config["ai_material_outreach_sensitivity"],
            task={
                "preface_mode": "ai" if config.get("ai_material_outreach_preface_enabled") else "none",
                "ai_preface_goal": config.get("ai_material_outreach_preface_goal", DEFAULT_AI_PREFACE_GOAL),
                "ai_preface_intensity": config.get("ai_material_outreach_preface_intensity", ""),
                "ai_preface_extra_instruction": "",
            },
            target=target,
        )
        try:
            self._record_ai_outreach_decision_api_request()
            reply = self._get_other_api().chat(message_text, prompt=prompt, history=history)
            decision = parse_ai_outreach_decision(reply)
        except Exception:
            return self._ai_outreach_queue_result(evaluation_attempted=True)
        if not decision.get("should_send"):
            return self._ai_outreach_queue_result(evaluation_attempted=True)
        selected_material = self._select_ai_outreach_candidate_by_index(
            target_cards,
            decision.get("selected_index"),
        )
        if not selected_material:
            return self._ai_outreach_queue_result(evaluation_attempted=True)
        generated_preface = ""
        if config.get("ai_material_outreach_preface_enabled"):
            try:
                generated_preface = self._generate_material_outreach_ai_preface(
                    {
                        "preface_mode": "ai",
                        "ai_preface_goal": config.get("ai_material_outreach_preface_goal", DEFAULT_AI_PREFACE_GOAL),
                        "ai_preface_intensity": config.get("ai_material_outreach_preface_intensity", ""),
                        "ai_preface_extra_instruction": "",
                    },
                    target,
                    selected_material,
                    send_mode=str(source or "detection_scan").strip() or "detection_scan",
                    send_strategy=decision.get("send_strategy", ""),
                )
            except Exception:
                return self._ai_outreach_queue_result(evaluation_attempted=True)
        with self._material_outreach_runtime_lock():
            queue_records = self._load_ai_pending_queue()
            latest_send_records = self._load_material_send_records()
            latest_target_cards = build_ai_outreach_candidates_for_target(candidate_cards, latest_send_records, target)
            gate = evaluate_ai_outreach_gate(
                config,
                is_private_ai_reply=True,
                target=target,
                candidate_cards=latest_target_cards,
                send_records=latest_send_records,
                queue_records=queue_records,
            )
            if not gate.get("allowed"):
                return self._ai_outreach_queue_result(evaluation_attempted=True)
            latest_selected_material = self._select_ai_outreach_candidate_by_index(
                latest_target_cards,
                decision.get("selected_index"),
            )
            if not latest_selected_material:
                return self._ai_outreach_queue_result(evaluation_attempted=True)
            record = build_ai_pending_record(
                target,
                latest_selected_material,
                decision,
                config,
                chat_name=target,
                random_delay_seconds=random.randint,
                queue_id_factory=lambda: f"aiq_{uuid.uuid4().hex[:8]}",
                generated_preface=generated_preface,
            )
            queue_records.append(record)
            self._save_ai_pending_queue(queue_records)
        return self._ai_outreach_queue_result(
            evaluation_attempted=True,
            queued=True,
            queue_id=record.get("queue_id"),
        )

    def _queue_ai_material_outreach_for_target(self, target, *, source="detection_scan"):
        target = str(target or "").strip()
        if not target:
            return self._ai_outreach_queue_result()
        history = []
        memory_manager = getattr(self, "memory_manager", None)
        if memory_manager is not None and hasattr(memory_manager, "get_messages"):
            try:
                count = max(0, int(getattr(self.config, "memory_context_count", 20) or 0))
                raw_limit = self._memory_context_raw_limit(count)
                raw_history = list(memory_manager.get_messages(
                    target,
                    raw_limit,
                    chat_type="private",
                ) or [])
                history = build_model_visible_history(
                    raw_history,
                    message_limit=count,
                )
            except Exception:
                history = []
        message_text = (
            f"当前没有新的好友来信，但刚到新的判定时机。请结合最近对话和当前关系状态，"
            f"判断现在是否适合主动联系 {target}，并选择最适合顺手转发的素材。"
        )
        return self._run_ai_material_outreach_evaluation(
            target,
            message_text=message_text,
            history=history,
            source=source,
        )

    def _process_ai_material_outreach_detection_scan(self, now=None):
        now = now or datetime.now()
        config = self._current_ai_material_outreach_config()
        state = self._load_ai_detection_state()
        if not config.get("ai_material_outreach_switch") or not state:
            return state
        for target in list(state.keys()):
            expected_record = normalize_ai_detection_record(state.get(target) or {})
            if self._ai_outreach_daily_limit_reached(target, now=now):
                self._clear_ai_detection_target_if_snapshot(target, expected_record)
                continue
            if not should_trigger_ai_detection(
                target,
                {target: expected_record},
                interval_minutes=config["ai_material_outreach_detection_interval_minutes"],
                message_threshold=config["ai_material_outreach_detection_message_threshold"],
                now=now,
            ):
                continue
            result = self._queue_ai_material_outreach_for_target(target, source="detection_scan")
            if result.get("evaluation_attempted"):
                self._clear_ai_detection_target_if_snapshot(target, expected_record)
        return self._load_ai_detection_state()

    def _record_private_reply_friend_message_for_ai_outreach(self, target, *, now=None):
        target = str(target or "").strip()
        if not target:
            return {}
        now = now or datetime.now()
        state = self._mutate_ai_detection_state(
            lambda current_state: record_ai_detection_message(
                target,
                current_state,
                now=now,
            )
        )
        return normalize_ai_detection_record(state.get(target) or {})

    def _should_run_ai_outreach_detection_for_private_reply(self, target, *, now=None, state=None):
        config = self._current_ai_material_outreach_config()
        state = state if isinstance(state, dict) else self._load_ai_detection_state()
        return should_trigger_ai_detection(
            target,
            state,
            interval_minutes=config["ai_material_outreach_detection_interval_minutes"],
            message_threshold=config["ai_material_outreach_detection_message_threshold"],
            now=now,
        )

    def _select_ai_outreach_candidate_by_index(self, candidate_cards, selected_index):
        try:
            selected_index = int(selected_index)
        except (TypeError, ValueError):
            return None
        for item in candidate_cards or []:
            try:
                if int(item.get("index")) == selected_index:
                    return item
            except (TypeError, ValueError):
                continue
        return None

    def _build_ai_outreach_decision_prompt(self, candidate_cards, sensitivity, *, task=None, target=""):
        system = getattr(self, "prompt_system", None)
        if system is None:
            system = self._init_prompt_system()
        return system.render_template_prompt(
            MATERIAL_OUTREACH_DECISION_PROMPT_FILE,
            target,
            {
                "candidate_cards_json": json.dumps(list(candidate_cards or []), ensure_ascii=False, indent=2),
                "sensitivity": describe_ai_outreach_sensitivity(sensitivity),
            },
            required_placeholders=(
                "{{candidate_cards_json}}",
                "{{sensitivity}}",
            ),
        )

    def _build_material_outreach_preface_prompt(self, task, target, material, *, send_mode="task_outreach", send_strategy=""):
        task = normalize_material_outreach_preface_config(task)
        material = material if isinstance(material, dict) else {}
        system = getattr(self, "prompt_system", None)
        if system is None:
            system = self._init_prompt_system()
        return system.render_template_prompt(
            MATERIAL_OUTREACH_PREFACE_PROMPT_FILE,
            target,
            {
                "material_type": str(material.get("type") or material.get("type_bucket") or "").strip(),
                "material_preview": str(material.get("content_preview") or "").strip(),
                "material_ownership": str(material.get("ownership") or "我的作品").strip(),
                "material_copy_note": str(material.get("copy_note") or "").strip(),
                "ai_preface_goal": str(task.get("ai_preface_goal") or "").strip() or DEFAULT_AI_PREFACE_GOAL,
                "ai_preface_intensity": str(task.get("ai_preface_intensity") or "").strip(),
                "ai_preface_extra_instruction": str(task.get("ai_preface_extra_instruction") or "").strip(),
                "send_strategy": str(send_strategy or "").strip(),
            },
            required_placeholders=(
                "{{material_type}}",
                "{{material_preview}}",
                "{{material_ownership}}",
                "{{material_copy_note}}",
                "{{ai_preface_goal}}",
                "{{ai_preface_intensity}}",
                "{{ai_preface_extra_instruction}}",
                "{{send_strategy}}",
            ),
        ).strip()

    def _parse_material_outreach_preface_reply(self, text):
        raw = str(text or "").strip()
        if not raw:
            return ""
        fence_match = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
        if fence_match:
            raw = fence_match.group(1).strip()
        cleaned = clean_ai_reply_text(raw).strip()
        if not cleaned:
            return ""
        if cleaned[:1] in {'"', "'"} and cleaned[-1:] == cleaned[:1]:
            cleaned = cleaned[1:-1].strip()
        if cleaned.startswith("{") and cleaned.endswith("}"):
            try:
                payload = json.loads(cleaned)
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                for key in ("preface", "text", "content", "message"):
                    value = str(payload.get(key) or "").strip()
                    if value:
                        cleaned = value
                        break
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        return " ".join(lines).strip()

    def _generate_material_outreach_ai_preface(self, task, target, material, *, send_mode="task_outreach", send_strategy=""):
        target = str(target or "").strip()
        material = material if isinstance(material, dict) else {}
        prompt = self._build_material_outreach_preface_prompt(
            task,
            target,
            material,
            send_mode=send_mode,
            send_strategy=send_strategy,
        )
        history = []
        memory_manager = getattr(self, "memory_manager", None)
        if memory_manager is not None and hasattr(memory_manager, "get_messages"):
            try:
                count = max(0, int(getattr(self.config, "memory_context_count", 20) or 0))
                raw_limit = self._memory_context_raw_limit(count)
                raw_history = list(memory_manager.get_messages(
                    target,
                    raw_limit,
                    chat_type="private",
                ) or [])
                history = build_model_visible_history(
                    raw_history,
                    message_limit=count,
                )
            except Exception:
                history = []
        title = str(material_title(material) or "").strip()
        message_text = "\n".join(
            part
            for part in (
                f"目标：{target}" if target else "",
                f"素材：{title}" if title else "",
                str(material.get("content_preview") or "").strip(),
            )
            if part
        ).strip()
        if str(send_mode or "").strip() in {"ai_chat_outreach", "detection_scan"}:
            self._record_ai_outreach_preface_api_request()
        else:
            self._record_material_preface_api_request()
        reply = self._get_other_api().chat(message_text, prompt=prompt, history=history)
        preface = self._parse_material_outreach_preface_reply(reply)
        if not preface:
            raise ValueError("AI 未生成可用附加文案")
        return preface

    def _queue_ai_material_outreach_for_private_reply(self, chat, message, history):
        return self._run_ai_material_outreach_evaluation(
            chat.who,
            message_text=message.content,
            history=history,
            source="ai_chat_outreach",
        )

    def _find_material_by_stable_signature(self, stable_signature):
        stable_signature = str(stable_signature or "").strip()
        materials = self._load_material_outreach_materials()
        for material in materials:
            if str(material.get("stable_signature") or "").strip() == stable_signature and material.get("status", "active") == "active":
                return material
        return None

    def _send_ai_material_outreach_record(self, record, *, now=None):
        now = now or datetime.now()
        material = self._find_material_by_stable_signature(record.get("stable_signature"))
        if material is None and record.get("material_source"):
            self._rebuild_material_pool_for_source(record.get("material_source"))
            material = self._find_material_by_stable_signature(record.get("stable_signature"))
        if material is None:
            error = "没有可用素材"
            self._append_material_send_record(
                build_send_record(
                    AI_AUTO_OUTREACH_TASK_ID,
                    record.get("material_id"),
                    record.get("material_type"),
                    record.get("target"),
                    False,
                    now=now,
                    error=error,
                    preface=record.get("preface", ""),
                    material_title=record.get("material_title", ""),
                    material_source=record.get("material_source", ""),
                    stable_signature=record.get("stable_signature", ""),
                    task_name=AI_AUTO_OUTREACH_TASK_NAME,
                    batch_id=record.get("batch_id") or record.get("queue_id") or record.get("run_id") or AI_AUTO_OUTREACH_TASK_ID,
                    run_id=record.get("run_id", ""),
                    targets_summary=record.get("targets_summary") or record.get("chat_name") or record.get("target", ""),
                    content_summary=record.get("preface") or record.get("content_summary", ""),
                    media_summary=record.get("media_summary", ""),
                    material_summary=record.get("material_summary") or "{}：{}".format(record.get("material_type", ""), record.get("material_title", "")).strip("："),
                    raw_targets=record.get("raw_targets") or [{"target": record.get("target", ""), "display_name": record.get("chat_name") or record.get("target", "")}],
                    raw_messages=record.get("raw_messages"),
                    raw_media=record.get("raw_media"),
                    raw_material=record.get("raw_material") or {
                        "material_id": record.get("material_id", ""),
                        "title": record.get("material_title", ""),
                        "type": record.get("material_type", ""),
                        "source": record.get("material_source", ""),
                    },
                ),
                limit=1000,
            )
            target_label = str(record.get("chat_name") or record.get("target") or "").strip() or "未知好友"
            log(level="WARNING", message=f"[AI自动转发] {target_label} 发送失败：{error}")
            return False, error
        material, runtime_message, _materials = self._material_runtime_message(material, refresh_missing=True)
        if runtime_message is None:
            error = "素材运行时句柄不存在"
            self._append_material_send_record(
                build_send_record(
                    AI_AUTO_OUTREACH_TASK_ID,
                    (material or {}).get("id", record.get("material_id")),
                    record.get("material_type"),
                    record.get("target"),
                    False,
                    now=now,
                    error=error,
                    preface=record.get("preface", ""),
                    material_title=record.get("material_title", ""),
                    material_source=record.get("material_source", ""),
                    stable_signature=record.get("stable_signature", ""),
                    task_name=AI_AUTO_OUTREACH_TASK_NAME,
                    batch_id=record.get("batch_id") or record.get("queue_id") or record.get("run_id") or AI_AUTO_OUTREACH_TASK_ID,
                    run_id=record.get("run_id", ""),
                    targets_summary=record.get("targets_summary") or record.get("chat_name") or record.get("target", ""),
                    content_summary=record.get("preface") or record.get("content_summary", ""),
                    media_summary=record.get("media_summary", ""),
                    material_summary=record.get("material_summary") or "{}：{}".format(record.get("material_type", ""), record.get("material_title", "")).strip("："),
                    raw_targets=record.get("raw_targets") or [{"target": record.get("target", ""), "display_name": record.get("chat_name") or record.get("target", "")}],
                    raw_messages=record.get("raw_messages"),
                    raw_media=record.get("raw_media"),
                    raw_material=record.get("raw_material") or {
                        "material_id": record.get("material_id", ""),
                        "title": record.get("material_title", ""),
                        "type": record.get("material_type", ""),
                        "source": record.get("material_source", ""),
                    },
                ),
                limit=1000,
            )
            target_label = str(record.get("chat_name") or record.get("target") or "").strip() or "未知好友"
            log(level="WARNING", message=f"[AI自动转发] {target_label} 发送失败：{error}")
            return False, error
        error = ""
        try:
            preface = record.get("preface") if record.get("preface_enabled") else ""
            success, error = self._forward_material_message(
                runtime_message,
                [record.get("target")],
                preface=preface,
                material_source=material.get("source") or record.get("material_source", ""),
                material_type=material.get("type_bucket") or material.get("type") or record.get("material_type"),
                material_title=record.get("material_title", ""),
                echo_source="ai_material_outreach",
            )
            if not success and self._material_forward_error_needs_refresh(error):
                log(level="WARNING", message=f"[AI素材转发] 素材句柄失效，已刷新来源子窗口后重试：{record.get('material_title', '')}")
                material, runtime_message, _materials = self._refresh_material_runtime_message(material)
                if runtime_message is not None:
                    success, error = self._forward_material_message(
                        runtime_message,
                        [record.get("target")],
                        preface=preface,
                        material_source=material.get("source") or record.get("material_source", ""),
                        material_type=material.get("type_bucket") or material.get("type") or record.get("material_type"),
                        material_title=record.get("material_title", ""),
                        echo_source="ai_material_outreach",
                    )
        except Exception as exc:
            success = False
            error = str(exc)
            if self._material_forward_error_needs_refresh(error):
                try:
                    log(level="WARNING", message=f"[AI素材转发] 素材句柄异常失效，已刷新来源子窗口后重试：{record.get('material_title', '')}")
                    material, runtime_message, _materials = self._refresh_material_runtime_message(material)
                    if runtime_message is not None:
                        preface = record.get("preface") if record.get("preface_enabled") else ""
                        success, error = self._forward_material_message(
                            runtime_message,
                            [record.get("target")],
                            preface=preface,
                            material_source=material.get("source") or record.get("material_source", ""),
                            material_type=material.get("type_bucket") or material.get("type") or record.get("material_type"),
                            material_title=record.get("material_title", ""),
                            echo_source="ai_material_outreach",
                        )
                except Exception as retry_exc:
                    error = str(retry_exc)
        self._append_material_send_record(
            build_send_record(
                AI_AUTO_OUTREACH_TASK_ID,
                material.get("id", record.get("material_id")),
                material.get("type_bucket") or material.get("type") or record.get("material_type"),
                record.get("target"),
                success,
                now=now,
                error=error,
                preface=record.get("preface", ""),
                material_title=record.get("material_title", ""),
                material_source=record.get("material_source", ""),
                stable_signature=record.get("stable_signature", ""),
                task_name=AI_AUTO_OUTREACH_TASK_NAME,
                batch_id=record.get("batch_id") or record.get("queue_id") or record.get("run_id") or AI_AUTO_OUTREACH_TASK_ID,
                run_id=record.get("run_id", ""),
                targets_summary=record.get("targets_summary") or record.get("chat_name") or record.get("target", ""),
                content_summary=record.get("preface") or record.get("content_summary", ""),
                media_summary=record.get("media_summary", ""),
                material_summary=record.get("material_summary") or "{}：{}".format(
                    material.get("type_bucket") or material.get("type") or record.get("material_type", ""),
                    record.get("material_title", ""),
                ).strip("："),
                raw_targets=record.get("raw_targets") or [{"target": record.get("target", ""), "display_name": record.get("chat_name") or record.get("target", "")}],
                raw_messages=record.get("raw_messages"),
                raw_media=record.get("raw_media"),
                raw_material=record.get("raw_material") or {
                    "material_id": material.get("id", record.get("material_id")),
                    "title": record.get("material_title", ""),
                    "type": material.get("type_bucket") or material.get("type") or record.get("material_type", ""),
                    "source": record.get("material_source", ""),
                },
            ),
            limit=1000,
        )
        target_label = str(record.get("chat_name") or record.get("target") or "").strip() or "未知好友"
        material_label = str(
            record.get("material_title")
            or material.get("content_preview")
            or material.get("id")
            or record.get("material_id")
            or ""
        ).strip() or "未命名素材"
        if success:
            log(level="INFO", message=f"[AI自动转发] {target_label} 已发送素材：{material_label}")
        else:
            log(level="WARNING", message=f"[AI自动转发] {target_label} 发送失败：{error or '未知原因'}")
        return success, error

    def _process_ai_material_outreach_queue(self, now=None):
        if self.is_stop_requested():
            return False
        now = now or datetime.now()
        with self._material_outreach_runtime_lock():
            queue_records = self._load_ai_pending_queue()
            if not self._current_ai_material_outreach_config().get("ai_material_outreach_switch"):
                changed = bool(cancel_ai_pending_records(queue_records))
                if changed:
                    self._save_ai_pending_queue(queue_records)
                return changed
            changed = bool(expire_ai_pending_records(queue_records, now=now))
            for record in due_ai_pending_records(queue_records, now=now):
                if self.is_stop_requested():
                    break
                success, error = self._send_ai_material_outreach_record(record, now=now)
                record["status"] = "sent" if success else "failed"
                record["error"] = str(error or "")
                changed = True
            if changed:
                self._save_ai_pending_queue(queue_records)
            return changed

    def _handle_material_source_message(self, chat, message):
        if not self._is_material_source_chat(chat):
            return False
        materials, entry, material_id = collect_material_source_message(
            self._load_material_outreach_materials(),
            chat.who,
            message,
            material_id_factory=lambda: f"mat_{uuid.uuid4().hex}",
            limit_map=getattr(self.config, "material_source_pool_limit_map", {}) or {},
        )
        if entry:
            self._save_material_outreach_materials(materials)
            self._material_runtime_messages[material_id] = message
            material_type = material_type_label(entry.get("type_bucket") or entry.get("type")) or "素材"
            title = material_title(entry)
            log_message = f"[素材转发] 已入池素材 来源：{chat.who}，类型：{material_type}"
            if title:
                log_message += f"，标题：{title}"
            log(message=log_message)
            return True
        if getattr(self.config, 'material_source_silent', True):
            log(message=f"[素材转发] 素材源 {chat.who} 非素材消息已静默跳过")
            return True
        return False

    # ----------------------------------------------------------
    # ----------------------------------------------------------



























    # ----------------------------------------------------------
    # 群组辅助功能
    # ----------------------------------------------------------

    def find_new_group_friend(self, msg, flag):
        return listening.find_new_group_friend(msg, flag)

    def send_group_welcome_msg(self, chat, message):
        return listening.send_group_welcome_msg(self, chat, message)

    # ----------------------------------------------------------
    # 新好友处理
    # ----------------------------------------------------------

    def is_image_path(self, s: str) -> bool:
        """
        判断字符串是否为有效的图片文件完整路径。
        支持 Windows（C:\\...）和 Unix（/home/...）风格路径。

        :param s: 待判断的字符串
        :return:  True 表示是图片路径，False 则不是
        """
        return is_image_path(s)

    def _existing_local_image_path(self, value: str) -> str:
        """返回真实存在的本地图片路径；用于合并队列把图片降级成文本时兜底识别。"""
        return existing_local_image_path(value, self._wxauto_download_dir())

    def _wxauto_download_dir(self):
        configured = str(getattr(WxParam, "DEFAULT_SAVE_PATH", "") or "").strip()
        if configured:
            return configured
        return os.path.join(_wxbot_runtime_base_dir(), WXAUTO_SAVE_DIR_NAME)

    def Pass_New_Friends(self):
        return listening.pass_new_friends(self)

    # ----------------------------------------------------------
    # 消息监听模式
    # ----------------------------------------------------------

    def listen_mode(self):
        return listening.listen_mode(self)

    def new_msg_get_plus(self, chat_records):
        return listening.new_msg_get_plus(chat_records)

    def add_chat_to_listen(self, chat, *, chat_type=None):
        return listening.add_chat_to_listen(self, chat, chat_type=chat_type)

    def is_chat_listened(self, chat, *, chat_type=None):
        return listening.is_chat_listened(self, chat, chat_type=chat_type)

    def ALLListen_mode(self, last_time, timeout=10):
        if not isinstance(self.wx, UIClientFacade):
            raise RuntimeError("全局监听必须通过微信 UI owner 执行")
        return listening.alllisten_mode(self, last_time=last_time, timeout=timeout)

    # ----------------------------------------------------------
    # 机器人生命周期
    # ----------------------------------------------------------

    def get_status(self):
        """
        暴露机器人运行状态数据，供 Web 状态面板采集。
        :return: 包含运行参数和统计数据的字典
        """
        uptime_secs = int((datetime.now() - self.start_time).total_seconds())
        days, rem = divmod(max(0, uptime_secs), 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _seconds = divmod(rem, 60)
        if days > 0:
            uptime_str = f"{days}天"
        elif hours > 0:
            uptime_str = f"{hours}小时"
        elif minutes > 0:
            uptime_str = f"{minutes}分钟"
        else:
            uptime_str = "1分钟"

        wx_nickname = None
        if self.wx:
            try:
                wx_nickname = self.wx.nickname
            except Exception:
                pass

        scheduled_tasks = getattr(self.config, "scheduled_message_task_list", []) or []
        scheduled_enabled = sum(
            1 for t in scheduled_tasks
            if isinstance(t, dict) and t.get('enabled', True)
        )
        api_configs = getattr(self.config, "api_configs", []) or []
        current_interface = self._get_current_chat_api_display_name()
        current_api = getattr(self.config, "current_api_config", None)
        if not isinstance(current_api, APIConfigSnapshot):
            api_index = int(getattr(self.config, "api_index", 0) or 0) if api_configs else 0
            current = api_configs[api_index] if api_configs and 0 <= api_index < len(api_configs) else {}
            current_api = build_api_config_snapshot(current)
        active_index = self._get_active_default_chat_api_index() if api_configs else 0
        active_model = ""
        if api_configs:
            active_model = str((api_configs[active_index] or {}).get("model", "") or "").strip()
        metrics_today = self.runtime_metrics_today()
        received_messages = int((metrics_today or {}).get("received_messages", 0) or 0)
        replied_messages = int((metrics_today or {}).get("reply_count", 0) or 0)
        api_calls = int((metrics_today or {}).get("api_calls", 0) or 0)
        chat_api_requests = int((metrics_today or {}).get("chat_api_calls", 0) or 0)

        return {
            "running":            self.run_flag,
            "version":            self.ver,
            "start_time":         self.start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "uptime":             uptime_str,
            "wx_nickname":        wx_nickname,
            "api_name":           current_api.sdk,
            "model":              active_model,
            "current_interface":  current_interface,
            "api_index":          active_index + 1,
            "api_total":          len(self.config.api_configs),
            "listen_mode":        "黑名单" if self.config.AllListen_switch else "白名单",
            "listen_count":       len(self.config.global_blacklist if self.config.AllListen_switch else self.config.listen_list),
            "chat_listen_only":    getattr(self.config, "chat_listen_only", False),
            "group_switch":       self.config.group_switch,
            "group_listen_only":   getattr(self.config, "group_listen_only", False),
            "group_count":        len(self.config.group),
            "msg_received":       received_messages,
            "msg_replied":        replied_messages,
            "api_request_count":   api_calls,
            "chat_api_requests":   chat_api_requests,
            "other_api_requests":  max(0, api_calls - chat_api_requests),
            "last_msg_time":      self.last_msg_time,
            "last_msg_sender":    self.last_msg_sender,
            "callback_is_die":    self.callback_is_die,
            "scheduled_switch":   scheduled_enabled > 0,
            "scheduled_count":    scheduled_enabled,
            "chat_keyword_switch":   self.config.chat_keyword_switch,
            "group_keyword_switch":  self.config.group_keyword_switch,
            "group_keyword_at_only": self.config.group_keyword_at_only,
            "keyword_count":         len(self.config.keyword_dict),
            "memory_switch":         getattr(self.config, "memory_switch", True),
            "memory_context_switch": getattr(self.config, "memory_context_switch", True),
            "memory_context_count":  getattr(self.config, "memory_context_count", 50),
            "chat_text_reply_limit_switch": getattr(self.config, "chat_text_reply_limit_switch", False),
            "chat_text_reply_limit_count": getattr(self.config, "chat_text_reply_limit_count", 50),
            "chat_text_reply_limit_hours": getattr(self.config, "chat_text_reply_limit_hours", 5),
            "group_text_reply_limit_switch": getattr(self.config, "group_text_reply_limit_switch", False),
            "group_text_reply_limit_count": getattr(self.config, "group_text_reply_limit_count", 50),
            "group_text_reply_limit_hours": getattr(self.config, "group_text_reply_limit_hours", 5),
            "pause_chat_reply":      self._pause_chat_reply or getattr(self.config, "chat_listen_only", False),
            "pause_group_reply":     self._pause_group_reply or getattr(self.config, "group_listen_only", False),
            **listening.listener_recovery_snapshot(self),
        }

    def _request_wxbot_stop_cleanup(self):
        lock = getattr(self, "_stop_cleanup_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._stop_cleanup_lock = lock
        with lock:
            if bool(getattr(self, "_stop_cleanup_done", False)):
                return True
            self._ensure_stop_requested_event().set()
            self.run_flag = False
            cleanup_steps = [
                ("取消私聊与语音定时器", self._cancel_pending_private_message_timers),
                ("清理群聊业务队列", self._clear_group_message_pipelines),
                ("停止会话记忆后台任务", self._clear_chat_memory_background_state),
            ]
            coordinator = getattr(self, "_reply_delivery_coordinator", None)
            if coordinator is not None:
                cleanup_steps.append(("停止回复投递", coordinator.stop))
            store = getattr(self, "_message_store", None)
            if store is not None:
                cleanup_steps.append(("取消未提交回复", store.cancel_unclaimed_on_shutdown))
            owner = getattr(self, "_ui_owner", None)
            if owner is not None:
                cleanup_steps.append(("取消待执行微信动作", owner.cancel_pending))

            errors = []
            for label, cleanup in cleanup_steps:
                try:
                    cleanup()
                except Exception as exc:
                    errors.append(f"{label}: {exc}")
            if errors:
                raise RuntimeError("；".join(errors))
            self._stop_cleanup_done = True
            return True

    def stop_wxbot(self):
        """Request a stop without waiting for a potentially stuck UI call."""
        try:
            self._request_wxbot_stop_cleanup()
            log(level="WARNING", message='siver_wxbot正在停止！！')
            return True
        except Exception as e:
            nickname = getattr(getattr(self, "wx", None), "nickname", "wxbot")
            self.is_err(nickname + ' wxbot机器人关闭程序执行出错！！', e)
            return False

    def _finish_wxbot_stop(self):
        """Run the real listener shutdown on the robot thread after its loop exits."""
        try:
            self._request_wxbot_stop_cleanup()
        except Exception as exc:
            log(level="ERROR", message=f"机器人停止前清理未完全成功：{exc}")
        owner = getattr(self, "_ui_owner", None)
        if owner is not None:
            try:
                owner.call_shutdown(wechat_ui_actions.UI_CALL_WAIT_TIMEOUT)
            finally:
                watchdog = getattr(self, "_ui_watchdog", None)
                if watchdog is not None:
                    watchdog.stop()
                self._ui_ingress_stop.set()
                owner.stop(cancel_pending=True)
                self._ui_owner = None
            log(level="WARNING", message='siver_wxbot安全退出！！')
            return True
        listener = getattr(self, "wx", None)
        if listener and hasattr(listener, "StopListening"):
            listener.StopListening()
        log(level="WARNING", message='siver_wxbot安全退出！！')
        return True

    def _run_wxbot_main(self):
        """
        机器人主运行函数：
        - 校验 wxautox 授权
        - 初始化微信监听器
        - 进入主循环，依次执行：离线检测、新好友检测、全局监听/定时任务
        """
        # self.key_pass(2025, 6, 20, 0, 0, 0)  # 打包保护锁（按需启用）
        # 激活授权校验
        if self.wxautox_activate_check():
            log(level="DEBUG", message="wxautox已激活")
        else:
            log(level="ERROR", message="wxautox未激活，请购买激活后再运行程序！！")
            log(level="ERROR", message="购买激活地址：https://www.siverking.online/static/img/siver_wx.jpg")
            self._notify_startup_status(False, "wxautox 未激活，请激活后再启动机器人")
            return False

        # 初始化微信监听器
        try:
            log(message="启动阶段：正在初始化微信监听器")
            self.init_wx_listeners()
            log(level="DEBUG", message="启动阶段：已同步面板状态")

            wait_time      = 3   # 主循环每 3 秒轮询一次
            check_interval = 10  # 每 10 次循环执行一次离线检测
            check_counter      = 0
            check_new_counter  = 0
            last_time          = time.time()
            log(level="SUCCESS", message='启动阶段：监听器已就绪，开始接收消息')
            if self.is_stop_requested():
                log(level="WARNING", message="启动过程中收到停止请求，已停止进入监听")
                self.run_flag = False
                self._notify_startup_status(False, "机器人启动过程中已被停止")
                return False
            self.run_flag = True
            self._notify_startup_status(True, "机器人已启动并进入监听")
        except Exception as e:
            print(traceback.format_exc())
            log(level="ERROR", message=f"启动阶段：微信监听器初始化失败，{e}")
            log(level="ERROR", message="启动建议：先检查微信是否已登录、主窗口是否正常显示")
            log(level="ERROR", message="启动建议：仍不行时，重启微信和面板，再检查 wx 版本")
            self.run_flag = False
            self._notify_startup_status(False, f"初始化微信监听器失败：{e}")

        # 主循环
        while self.run_flag:
            try:
                if self.is_stop_requested():
                    break
                recovery_state = self._process_listener_auto_recovery()
                if recovery_state == "waiting":
                    self._wait_or_stop_requested(wait_time)
                    continue
                if recovery_state == "failed":
                    self.stop_wxbot()
                    log(level="ERROR", message="监听器自动恢复失败，主线程即将退出")
                    break

                try:
                    self._maybe_reconcile_listener_subwindows(retry_count=1)
                except Exception as e:
                    if self._arm_listener_auto_recovery(e, source="固定监听巡检"):
                        continue
                    log(level="WARNING", message=f"固定监听巡检出错：{e}")

                # ---- 离线检测模块（每 check_interval 次循环执行一次）----
                if self.is_stop_requested():
                    break
                check_counter += 1
                if check_counter >= check_interval:
                    try:
                        if self.callback_is_die:
                            log(level="ERROR", message="检测到回调函数出错，主线程即将统一停止")
                            break
                        if not self.check_wechat_window():
                            # 微信离线，阻塞等待人工处理
                            self.is_err(self.wx.nickname + " wxbot监听出错！！微信可能已被弹出登录！！在线检查失败！！")
                            self.stop_wxbot()
                            log(level="ERROR", message=f"微信 {self.wx.nickname} 已被弹出登录！！请检查微信是否登录！！")
                            break
                    except Exception as e:
                        if self._arm_listener_auto_recovery(e, source="在线检查"):
                            check_counter = 0
                            continue
                        self.is_err(self.wx.nickname + " wxbot监听出错！！微信可能已被弹出登录！！在线检查失败！！", e)
                        self.stop_wxbot()
                        log(level="ERROR", message=f"微信 {self.wx.nickname} 已被弹出登录！！请检查微信是否登录！！")
                        break
                    check_counter = 0

                # ---- 新好友检测模块（随机检查，间隔由配置决定）----
                if self.is_stop_requested():
                    break
                if self.config.new_friend_switch:
                    # 将秒数阈值除以循环周期得到循环次数（取整，最小1次）
                    check_new_friend_time_MIN = max(1, int(self.config.new_friend_check_min / wait_time))
                    check_new_friend_time_MAX = max(check_new_friend_time_MIN, int(self.config.new_friend_check_max / wait_time))
                    check_new_counter += 1
                    if check_new_counter >= random.randint(check_new_friend_time_MIN, check_new_friend_time_MAX):
                        try:
                            self.Pass_New_Friends()
                            # log(message="检查新好友完成")
                        except Exception as e:
                            self.is_err(self.wx.nickname + "  智能客服bot监听新好友出错！！请检查程序！！", e)
                        check_new_counter = 0

                # ---- 全局监听模式（黑名单模式下启用）----
                if self.is_stop_requested():
                    break
                if self.config.AllListen_switch:
                    try:
                        last_time = self.ALLListen_mode(last_time=last_time)
                    except Exception as e:
                        if self._arm_listener_auto_recovery(e, source="全局监听模式"):
                            continue
                        if not self.run_flag:
                            log(level="ERROR", message=str(e) + "\n全局模式出错！！请检查程序！！")

                # ---- 运行中任务配置热更新（不打断当前执行中的动作）----
                if self.is_stop_requested():
                    break
                self._process_pending_runtime_task_reload()

                # ---- 统一时间内核扫描（固定任务 / 点赞间隔任务）----
                try:
                    if self.is_stop_requested():
                        break
                    self._process_unified_runtime_tasks()
                except Exception as e:
                    log(level="ERROR", message=f"统一时间任务扫描出错：{e}")

                try:
                    if self.is_stop_requested():
                        break
                    self._process_material_outreach_preface_queue()
                except Exception as e:
                    log(level="ERROR", message=f"素材转发 AI 文案预生成队列处理出错：{e}")

                try:
                    if self.is_stop_requested():
                        break
                    self._process_ai_material_outreach_queue()
                except Exception as e:
                    log(level="ERROR", message=f"AI 自动素材转发队列处理出错：{e}")

                try:
                    if self.is_stop_requested():
                        break
                    self._process_ai_material_outreach_detection_scan()
                except Exception as e:
                    log(level="ERROR", message=f"AI 自动素材转发判定扫描出错：{e}")

                try:
                    if self.is_stop_requested():
                        break
                    self._check_relationship_auto_scan()
                except Exception as e:
                    log(level="ERROR", message=f"关系扫描模块出错：{e}")

                try:
                    if self.is_stop_requested():
                        break
                    self._check_friend_request_auto_run()
                except Exception as e:
                    log(level="ERROR", message=f"好友申请模块出错：{e}")

                if self._contact_directory_auto_maintenance_enabled():
                    try:
                        if self.is_stop_requested():
                            break
                        self._check_contact_directory_auto_maintenance()
                    except Exception as e:
                        log(level="ERROR", message=f"通讯录自动维护模块出错：{e}")

            except Exception as e:
                self.is_err(
                    self.wx.nickname + " wxbot消息处理出错！！微信可能已被弹出登录！！处理监听失败！！",
                    e,
                )
                self.run_flag = False

            self._wait_or_stop_requested(wait_time)

    def main(self):
        try:
            return self._run_wxbot_main()
        finally:
            log(level="WARNING", message='siver_wxbot主线程安全退出，正在退出监听...')
            try:
                self._finish_wxbot_stop()
            except Exception as e:
                nickname = getattr(getattr(self, "wx", None), "nickname", "wxbot")
                self.is_err(nickname + ' wxbot停止监听失败！！', e)

    def run(self):
        """启动机器人（对外暴露的入口函数）"""
        self.main()

    def stop(self):
        """停止机器人（对外暴露的入口函数）"""
        self.stop_wxbot()


# ============================================================
# 程序入口
# ============================================================
if __name__ == "__main__":
    bot = WXBot()
    bot.run()
