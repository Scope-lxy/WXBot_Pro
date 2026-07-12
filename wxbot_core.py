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
from extension import webhook as webhook_send
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
    CHAT_MEMORY_DEFAULT_PROTECTED_RECENT_COUNT,
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
from core import runtime_chat_state, wechat_ui_actions
from core.wechat_ui_runtime import OwnedChat, UIClientFacade, WeChatUIRuntime
from core.ui_delivery_journal import UIDeliveryJournal
from core.unanswered_inbound import UnansweredInboundStore
from core.chat_history_format import (
    build_model_visible_history,
    format_history_message,
)
from core.memory_context_repair import (
    DEFAULT_ANCHOR_RECENT_COUNT,
    DEFAULT_CONTEXT_REPAIR_COOLDOWN_SECONDS,
    DEFAULT_LOCAL_HISTORY_LIMIT,
    DEFAULT_VISIBLE_LIMIT,
    current_message_found_near_tail,
    normalize_wechat_snapshot,
    snapshot_messages_through_current,
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
    VoiceSessionManager,
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
from feature.custom_forward import (
    is_custom_forward_source,
    iter_custom_forward_listen_sources,
)
from feature import contacts, friend_request, listening, message_routing, relationship_scan, takeover_runtime
from feature import admin_forward_flow, admin_moments_flow


PENDING_VISUAL_CONTEXT_TTL_SECONDS = 600
PENDING_VISUAL_DIRECT_REFERENCE_RE = re.compile(
    r"("
    r"图|照|相|截|屏|画|码|表情|菜单|票|单|"
    r"\b(?:image|picture|photo|photograph|screenshot|screen\s*shot|pic|img|meme|poster|"
    r"qr\s*code|barcode)\b"
    r")",
    re.IGNORECASE,
)
PENDING_VISUAL_CONTEXT_REFERENCE_RE = re.compile(
    r"("
    r"这|那|刚|东西|张|上|前|后|"
    r"\b(?:this|this\s+one|that|that\s+one|it|above|below|previous|last\s+one|next|the\s+one\s+above)\b"
    r")",
    re.IGNORECASE,
)
PENDING_VISUAL_ACTION_RE = re.compile(
    r"("
    r"看|听|说|读|写|什么|识别|解释|意思|含义|翻译|描述|分析|提取|"
    r"\b(?:ocr|read|describe|analy[sz]e|recognize|identify|transcribe|extract|translate|"
    r"explain|caption|what(?:'s| is)|what does|mean|say|says|text)\b"
    r")",
    re.IGNORECASE,
)
PENDING_VISUAL_STANDALONE_ACTION_RE = re.compile(
    r"("
    r"看|听|说|读|写|识别|解释|意思|含义|翻译|描述|分析|提取|"
    r"\b(?:ocr|read|describe|analy[sz]e|recognize|identify|transcribe|extract|translate|"
    r"explain|caption|mean|say|says|text)\b"
    r")",
    re.IGNORECASE,
)
PRIVATE_REPLY_ECHO_DEDUPE_SECONDS = 60
PRIVATE_OUTBOUND_ECHO_NON_TEXT_SECONDS = 60
PRIVATE_OUTBOUND_ECHO_UNKNOWN_SECONDS = 30
PRIVATE_OUTBOUND_ECHO_MAX_PER_CHAT = 20
PRIVATE_OUTBOUND_REPORTED_FAILED_CALLBACK_SECONDS = 2
PRIVATE_OUTBOUND_HISTORY_STOP_WAIT_SECONDS = 5
CHAT_MEMORY_BACKGROUND_INTERVAL_SECONDS = 30
PRIVATE_MESSAGE_PIPELINE_MAX_QUEUED_BATCHES = 1
from feature import runtime_task_runner
from feature.admin_commands import dispatch_admin_command
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
from feature.moments_tasks import (
    STATUS_EXECUTED,
    STATUS_PENDING,
    append_draft_image,
    append_draft_text,
    clear_active_draft,
    copy_moments_admin_upload,
    create_empty_draft,
    delete_managed_moments_uploads,
    deserialize_moments_task_collection,
    draft_has_material,
    load_active_draft,
    moments_task_has_ai_candidates,
    moments_task_from_admin_draft,
    moments_task_publish_text,
    moments_visibility_to_privacy,
    normalize_moments_task,
    parse_moments_candidates,
    queue_moments_task,
    render_preview_reply,
    save_active_draft,
    serialize_moments_task_collection,
    split_moments_task_storage,
)
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
    execute_scheduled_message_task,
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
MOMENTS_CAPTION_PROMPT_FILE = "moments_caption.md"
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
        self._use_ui_owner = True
        self._ui_owner = None
        self._ui_runtime = None
        self._ui_watchdog = None
        self._ui_identity = {}
        self._ui_ingress_queue = Queue()
        self._ui_ingress_stop = threading.Event()
        self._ui_ingress_thread = None
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
        self._material_source_chats = {}
        self._material_source_read_strategies = {}
        self._random_material_outreach_state = {}
        self._runtime_task_reload_lock = threading.RLock()
        self._runtime_task_reload_requested = False
        self._set_material_outreach_namespace()
        self._set_admin_moments_draft_namespace()
        self._set_admin_forward_draft_namespace()
        self._pause_chat_reply        = False  # 暂停私聊 AI 自动回复标志
        self._pause_group_reply       = False  # 暂停群聊 AI 自动回复标志
        self._pause_chat_reply_users  = set()  # 单个好友人工接管暂停列表
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
        self._incoming_seen_lock = threading.Lock()
        self._incoming_seen_ids = {}
        self._incoming_seen_fingerprints = {}
        self._chat_merge_lock = threading.Lock()
        self._material_source_read_locks = {}
        self._material_source_read_locks_guard = threading.Lock()
        self._last_incoming_message_at = 0.0
        self._private_message_pipelines = {}
        self._private_message_sequence_by_chat = {}
        self._memory_context_repair_startup_done = set()
        self._memory_context_repair_restore_pending = set()
        self._memory_context_repair_last_at = {}
        self._memory_context_repair_lock = threading.Lock()
        self._pending_private_voice_transcription = {}
        self._lightweight_send_queue_lock = threading.RLock()
        self._lightweight_send_queue = {}
        self._lightweight_send_queue_flushing = False
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

    def _enqueue_ui_message(self, conversation, message):
        if self.is_stop_requested():
            return False
        if not isinstance(conversation, ConversationRef) or not isinstance(message, MessageEnvelope):
            raise TypeError("微信回调只能提交纯数据消息")
        is_private = (
            str(getattr(conversation, "chat_type", "private") or "private").lower() != "group"
            and str(getattr(conversation, "who", "") or "") not in set(getattr(self.config, "group", []) or [])
        )
        if is_private and getattr(message, "attr", "") == "friend":
            if not self._mark_message_seen(conversation.who, message):
                return True
            if not self._mark_message_content_fingerprint_seen(conversation.who, message):
                return True
            message._wxbot_seen_at_ingress = True
            message._wxbot_sequence_advanced = True
            self._next_private_message_sequence(conversation.who)
        elif (
            is_private
            and getattr(message, "attr", "") == "self"
            and conversation.who != getattr(self.config, "cmd", "")
        ):
            if not self._mark_message_seen(conversation.who, message):
                return True
            message._wxbot_seen_at_ingress = True
            outbound_echo = self._consume_private_outbound_echo(
                conversation.who,
                message=message,
                return_match=True,
            )
            if outbound_echo:
                message._wxbot_private_outbound_echo = True
                if outbound_echo.get("reservation_id"):
                    message._wxbot_outbound_echo_reservation = outbound_echo["reservation_id"]
                if (
                    outbound_echo.get("duplicate_reservation")
                    or outbound_echo.get("dedupe_only")
                    or outbound_echo.get("primary_memory_writer")
                ):
                    message._wxbot_private_outbound_echo_duplicate = True
                if outbound_echo.get("primary_memory_writer"):
                    message._wxbot_private_outbound_echo_primary = True
                if outbound_echo.get("memory_persisted"):
                    message._wxbot_private_reply_persisted_echo = True
            elif self._matches_private_reply_runtime_echo(conversation.who, getattr(message, "content", "")):
                message._wxbot_runtime_reply_echo = True
            else:
                message._wxbot_sequence_advanced = True
                self._next_private_message_sequence(conversation.who)
        self._ui_ingress_queue.put((conversation, message))
        return True

    def _run_ui_ingress(self):
        while not self._ui_ingress_stop.is_set():
            try:
                conversation, message = self._ui_ingress_queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                if not self._ui_owner.wait_for_contact_idle():
                    continue
                chat = OwnedChat(self._ui_owner, conversation.who, conversation.chat_type)
                self.message_handle_callback(message, chat)
            except Exception as exc:
                log(level="ERROR", message=f"微信消息业务队列处理失败：{exc}")
            finally:
                self._ui_ingress_queue.task_done()

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

    def _bootstrap_ui_owner(self, listeners):
        if self._ui_owner is not None:
            return dict(self._ui_identity)
        orphan_cleanup = contacts.cleanup_orphaned_contact_auto_collector()
        if orphan_cleanup.get("terminated"):
            log(level="WARNING", message=f"已清理遗留通讯录采集进程 PID {orphan_cleanup.get('pid')}")
        self._ui_ingress_stop.clear()
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
        )
        self._ui_owner = wechat_ui_actions.WeChatUIOwner(
            self._ui_runtime.handlers(),
            conversation_version_provider=self._get_private_message_sequence,
            task_version_provider=self._current_ui_task_version,
            payload_preparer=self._prepare_ui_intent_payload,
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
                {"listeners": [{"name": str(name or "").strip()} for name in listeners if str(name or "").strip()]},
            ),
            wechat_ui_actions.UI_CALL_WAIT_TIMEOUT,
        )
        self._ui_identity = dict(identity or {})
        delivery_journal = UIDeliveryJournal(
            self.config.DATA_DIR,
            self._ui_identity.get("wx_id") or self._ui_identity.get("nickname") or "default",
        )
        recovered_deliveries = delivery_journal.freeze_interrupted()
        if recovered_deliveries:
            log(
                level="WARNING",
                message=f"微信发送恢复：{len(recovered_deliveries)} 条投递结果未知，已禁止自动重发",
            )
        self._ui_owner.set_delivery_journal(delivery_journal)
        self.wx = UIClientFacade(self._ui_owner, self._ui_identity)
        unanswered_store = UnansweredInboundStore(
            self.config.DATA_DIR,
            self._ui_identity.get("wx_id") or self._ui_identity.get("nickname") or "default",
        )
        self._unanswered_inbound_store = unanswered_store
        self._unanswered_inbound_context = threading.local()
        self._pending_unanswered_inbound_recovery = unanswered_store.recover_for_replay()
        return dict(self._ui_identity)

    def _drain_unanswered_inbound_recovery(self):
        recovered_inbound = list(getattr(self, "_pending_unanswered_inbound_recovery", []) or [])
        self._pending_unanswered_inbound_recovery = []
        for record in recovered_inbound:
            payload = dict(record.get("message") or {})
            message = MessageEnvelope(
                content=str(payload.get("content") or ""),
                original_content=str(payload.get("original_content") or payload.get("content") or ""),
                type=str(payload.get("type") or ""),
                sender=str(payload.get("sender") or ""),
                attr=str(payload.get("attr") or "friend"),
                id=payload.get("id") or "",
                hash=payload.get("hash") or "",
                hash_text=payload.get("hash_text") or "",
                time=payload.get("time") or "",
                _wxbot_ingress_source="unanswered_recovery",
                _wxbot_received_at=float(record.get("received_at") or time.time()),
            )
            message._wxbot_recovery_record_id = str(record.get("record_id") or "")
            chat_type = str(record.get("chat_type") or "private").strip().lower() or "private"
            message._wxbot_recovery_chat_type = chat_type
            message._wxbot_unanswered_recovery = True
            if chat_type == "group":
                message._skip_memory = True
                self._enqueue_ui_message(
                    ConversationRef(str(record.get("conversation") or ""), "group"),
                    message,
                )
            else:
                self._enqueue_private_message_for_ai(
                    OwnedChat(self._ui_owner, str(record.get("conversation") or "")),
                    message,
                )
        if recovered_inbound:
            log(level="WARNING", message=f"消息恢复：已重新排队 {len(recovered_inbound)} 条 AI 生成中断消息")
        return len(recovered_inbound)

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

    @staticmethod
    def _ui_task_definition_version(definition):
        payload = json.dumps(definition or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        version = int.from_bytes(hashlib.blake2s(payload.encode("utf-8"), digest_size=8).digest(), "big")
        return version or 1

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
                return self._ui_task_definition_version(definition)
        if category == "moments":
            storage = self._moments_task_storage()
            tasks = storage.load_tasks() if storage is not None else None
            if tasks is None:
                tasks = getattr(self.config, "moments_task_list", []) or []
            for task in tasks:
                if not isinstance(task, dict) or str(task.get("id") or "").strip() != task_id:
                    continue
                definition, _runtime, _history = split_moments_task_storage(task)
                return self._ui_task_definition_version(definition)
        if category == "material_outreach":
            storage = self._material_outreach_storage()
            tasks = storage.load_tasks() if storage is not None else None
            if tasks is None:
                tasks = getattr(self.config, "material_outreach_list", []) or []
            for task in tasks:
                current_id = str(task.get("id") or task.get("task_id") or "").strip() if isinstance(task, dict) else ""
                if current_id == task_id:
                    return self._ui_task_definition_version(task)
        if category in {"contact_auto", "new_friend", "keyword", "group_welcome", "custom_forward"}:
            definition = self._config_ui_task_definition(category)
            return self._ui_task_definition_version(definition) if definition else 0
        if category == "relationship_auto":
            state = relationship_scan.load_bot_state(self)
            settings = relationship_scan.normalize_settings(state.get("settings"))
            return self._ui_task_definition_version(settings)
        if category == "friend_request":
            state = friend_request.load_state(self.config.DATA_DIR, getattr(self, "wx_id", "") or "default")
            candidate = next((
                item for item in (state.get("candidates") or [])
                if isinstance(item, dict) and str(item.get("candidate_id") or "").strip() == task_id
            ), None)
            if candidate is None:
                return 0
            return self._ui_task_definition_version({
                "settings": friend_request.normalize_settings(state.get("settings")),
                "message_rules": state.get("message_rules") or [],
                "candidate": candidate,
            })
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
            "custom_forward": (
                "custom_forward_switch",
                "custom_forward_list",
            ),
        }.get(category, ())
        definition = {field: config_data.get(field) for field in fields}
        if category in {"keyword", "custom_forward"}:
            path_method = (
                "_keyword_rules_file" if category == "keyword" else "_custom_forward_rules_file"
            )
            field = "keyword_dict" if category == "keyword" else "custom_forward_list"
            fallback = {} if category == "keyword" else []
            path_provider = getattr(self.config, path_method, None)
            path = path_provider() if callable(path_provider) else None
            if path:
                try:
                    with open(path, "r", encoding="utf-8-sig") as handle:
                        loaded = json.load(handle)
                    expected_type = dict if category == "keyword" else list
                    definition[field] = loaded if isinstance(loaded, expected_type) else fallback
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    definition[field] = fallback
        return definition

    def _config_ui_task_guard(self, category):
        definition = self._config_ui_task_definition(category)
        if not definition:
            return "", 0
        return f"{category}:config", self._ui_task_definition_version(definition)

    def _relationship_auto_ui_guard(self, settings=None):
        settings = relationship_scan.normalize_settings(settings)
        return "relationship_auto:settings", self._ui_task_definition_version(settings)

    def _friend_request_ui_guard(self, state, candidate):
        candidate_id = str((candidate or {}).get("candidate_id") or "").strip()
        if not candidate_id:
            return "", 0
        definition = {
            "settings": friend_request.normalize_settings((state or {}).get("settings")),
            "message_rules": (state or {}).get("message_rules") or [],
            "candidate": dict(candidate or {}),
        }
        return f"friend_request:{candidate_id}", self._ui_task_definition_version(definition)

    def _scheduled_message_ui_guard(self, task):
        task_id = str((task or {}).get("id") or "").strip()
        if not task_id:
            return "", 0
        task_key = f"scheduled_message:{task_id}"
        definition, _runtime, _history = split_scheduled_message_task_storage(task)
        return task_key, self._ui_task_definition_version(definition)

    def _moments_ui_guard(self, task):
        task_id = str((task or {}).get("id") or "").strip()
        if not task_id:
            return "", 0
        task_key = f"moments:{task_id}"
        definition, _runtime, _history = split_moments_task_storage(task)
        return task_key, self._ui_task_definition_version(definition)

    def _material_outreach_ui_guard(self, task):
        task_id = str((task or {}).get("id") or (task or {}).get("task_id") or "").strip()
        if not task_id:
            return "", 0
        task_key = f"material_outreach:{task_id}"
        current_version = self._current_ui_task_version(task_key)
        return task_key, current_version or self._ui_task_definition_version(task)

    @staticmethod
    def _ui_message_payload(chat, message):
        return {
            "conversation": str(getattr(chat, "who", "") or ""),
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

    def _stable_inbound_delivery_id(self, prefix, chat, message):
        seed = "|".join([
            str(prefix or ""),
            str(getattr(chat, "who", "") or ""),
            str(getattr(message, "id", "") or ""),
            str(getattr(message, "hash", "") or ""),
            str(getattr(message, "hash_text", "") or ""),
            str(getattr(message, "time", "") or ""),
            str(getattr(message, "sender", "") or ""),
            str(getattr(message, "type", "") or ""),
            str(getattr(message, "original_content", "") or getattr(message, "content", "") or ""),
        ])
        return f"{prefix}:{hashlib.sha256(seed.encode('utf-8')).hexdigest()}"

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
            "task_key": task_key,
        })
        return self._ui_owner.call(
            wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.FORWARD,
                payload,
                task_version=task_version,
            ),
            wechat_ui_actions.UI_CALL_WAIT_TIMEOUT,
        )

    def _ui_quote_message(self, chat, message, text, *, at=""):
        payload = self._ui_message_payload(chat, message)
        payload.update({"text": str(text or ""), "at": str(at or ""), "delivery_id": str(uuid.uuid4())})
        return self._ui_owner.call(
            wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.QUOTE, payload),
            wechat_ui_actions.UI_CALL_WAIT_TIMEOUT,
        )

    def _resolve_group_unanswered(self, record_id, message):
        store = getattr(self, "_unanswered_inbound_store", None)
        if store is None:
            return
        if record_id:
            store.resolve(record_id)
        recovery_record_id = str(getattr(message, "_wxbot_recovery_record_id", "") or "")
        if recovery_record_id:
            store.resolve(recovery_record_id)

    def _run_contact_auto_maintenance_collector(
        self,
        *,
        start_name,
        start_identity="",
        count,
        timeout_seconds,
        run_kind="auto_maintenance",
    ):
        if getattr(self, "_ui_owner", None) is None:
            return contacts.run_contact_auto_maintenance_collector(
                start_name=start_name,
                start_identity=start_identity,
                count=50,
                timeout_seconds=300,
            )
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
        self._ensure_stop_requested_event().clear()

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
        try:
            self._record_replied_message_success(chat_name, chat_type=chat_type)
        except TypeError:
            self._record_replied_message_success()

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

    def _set_admin_moments_draft_namespace(self, wx_id=None):
        wx_id = resolve_account_id(wx_id, fallback_default=True)
        base_dir = str(account_area_dir(self.config.DATA_DIR, wx_id, "moments_drafts", create=True))
        os.makedirs(base_dir, exist_ok=True)
        self._moments_draft_file = os.path.join(base_dir, "active_draft.json")

    def _set_admin_forward_draft_namespace(self, wx_id=None):
        wx_id = resolve_account_id(wx_id, fallback_default=True)
        base_dir = str(account_area_dir(self.config.DATA_DIR, wx_id, "forward_drafts", create=True))
        os.makedirs(base_dir, exist_ok=True)
        self._forward_draft_file = os.path.join(base_dir, "active_draft.json")

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

    def _ensure_lightweight_send_queue_state(self):
        if not hasattr(self, "_lightweight_send_queue_lock"):
            self._lightweight_send_queue_lock = threading.RLock()
        if not hasattr(self, "_lightweight_send_queue") or self._lightweight_send_queue is None:
            self._lightweight_send_queue = {}
        if not hasattr(self, "_lightweight_send_queue_flushing"):
            self._lightweight_send_queue_flushing = False

    def _queue_lightweight_send(self, target, actions, *, source="", expected_sequence=None):
        target = str(target or "").strip()
        actions = [dict(item) for item in (actions or []) if isinstance(item, dict)]
        if not target or not actions:
            return False
        sequence = None
        if expected_sequence is not None:
            try:
                sequence = int(expected_sequence)
            except Exception:
                sequence = None
        self._ensure_lightweight_send_queue_state()
        with self._lightweight_send_queue_lock:
            self._lightweight_send_queue[target] = {
                "target": target,
                "actions": actions,
                "source": str(source or "").strip(),
                "queued_at": datetime.now().replace(microsecond=0).isoformat(),
                "delivery_id": str(uuid.uuid4()),
            }
            if sequence is not None:
                self._lightweight_send_queue[target]["expected_sequence"] = sequence
        return {"status": "queued", "message": "已进入轻量延后发送队列", "data": {"target": target}}

    def _ensure_target_listen_chat_for_send(self, target):
        target = str(target or "").strip()
        if not target:
            return None
        chat = runtime_chat_state.get_listen_chat(self, target)
        if runtime_chat_state.listen_chat_has_method(chat, "SendMsg") and self._listen_chat_matches_target(chat, target):
            return chat
        if chat:
            runtime_chat_state.remove_listen_chat(self, target)
        release_wechat_lock = wechat_ui_actions.try_acquire(self)
        if not release_wechat_lock:
            return None
        try:
            chat = runtime_chat_state.get_listen_chat(self, target)
            if runtime_chat_state.listen_chat_has_method(chat, "SendMsg") and self._listen_chat_matches_target(chat, target):
                return chat
            if chat:
                runtime_chat_state.remove_listen_chat(self, target)
            with warn_slow_wechat_ui_action(f"AddListenChat({target})"):
                result = self.wx.AddListenChat(nickname=target, callback=self.message_handle_callback)
            verified_chat = self._verified_send_chat(target, result)
            if verified_chat:
                runtime_chat_state.remember_listen_chat(self, target, verified_chat)
                return verified_chat
            log(level="WARNING", message=f"[轻量发送队列] 自动恢复监听子窗口失败：{target}，{result}")
            return None
        except Exception as exc:
            log(level="WARNING", message=f"[轻量发送队列] 自动恢复监听子窗口异常：{target}，{exc}")
            return None
        finally:
            release_wechat_lock()

    def _listen_chat_matches_target(self, chat, target):
        target = str(target or "").strip()
        if not target or not chat or isinstance(chat, dict):
            return False
        who = listening.subwindow_who(chat)
        return bool(who and who == target)

    def _verified_send_chat(self, target, candidate=None):
        target = str(target or "").strip()
        if not target:
            return None
        if runtime_chat_state.listen_chat_has_method(candidate, "SendMsg") and self._listen_chat_matches_target(candidate, target):
            return candidate
        return None

    def _send_lightweight_actions_to_child(
        self,
        target,
        actions,
        *,
        source="lightweight_send",
        expected_sequence=None,
        delivery_id="",
    ):
        if getattr(self, "_ui_owner", None) is not None:
            prepared_actions = [dict(action or {}) for action in actions or []]
            if not prepared_actions:
                return False
            try:
                sequence = int(expected_sequence or 0)
            except Exception:
                sequence = 0
            completed_results = []
            delivery_base = str(delivery_id or uuid.uuid4())
            for index, action in enumerate(prepared_actions):
                if source == "private_ai_reply":
                    if sequence and self._get_private_message_sequence(target) != sequence:
                        raise wechat_ui_actions.IntentCancelled("会话已有新消息")
                    if index > 0:
                        self._human_delay_for_reply_part(
                            part_text=str(action.get("text") or ""),
                            split_continuation=True,
                            is_last=(index == len(prepared_actions) - 1),
                            delay_enabled=bool(
                                getattr(getattr(self, "config", None), "chat_split_reply_switch", False)
                                and getattr(getattr(self, "config", None), "chat_split_reply_delay_switch", True)
                            ),
                        )
                echo_group_id = self._reserve_private_outbound_echo_for_action(target, action, source=source)
                try:
                    results = self._ui_owner.call(
                        wechat_ui_actions.UIIntent(
                            wechat_ui_actions.UIIntentKind.SEND_ACTIONS,
                            {
                                "conversation": target,
                                "delivery_id": f"{delivery_base}:{index}",
                                "actions": [action],
                            },
                            conversation_version=sequence,
                        ),
                        wechat_ui_actions.UI_CALL_WAIT_TIMEOUT,
                    )
                except wechat_ui_actions.ActionBatchInterrupted as exc:
                    partial = list(exc.completed_results)
                    if partial:
                        self._settle_private_outbound_echo_send(echo_group_id, partial[-1])
                    else:
                        self._discard_private_outbound_echo_group(echo_group_id)
                    raise
                except wechat_ui_actions.IntentCancelled:
                    self._discard_private_outbound_echo_group(echo_group_id)
                    raise
                except Exception:
                    self._settle_private_outbound_echo_send(echo_group_id, None)
                    raise
                action_results = list(results) if isinstance(results, (list, tuple)) else [results]
                action_result = action_results[-1] if action_results else True
                completed_results.append(action_result)
                self._settle_private_outbound_echo_send(echo_group_id, action_result)
            return completed_results[-1] if completed_results else True
        chat = runtime_chat_state.get_listen_chat(self, target)
        if not (runtime_chat_state.listen_chat_has_method(chat, "SendMsg") and self._listen_chat_matches_target(chat, target)):
            chat = self._ensure_target_listen_chat_for_send(target)
        if not chat:
            return False
        result = True
        with self._get_chat_send_lock(target):
            for action in actions or []:
                action_type = str((action or {}).get("type") or "").strip().lower()
                if action_type == "file":
                    path = str((action or {}).get("path") or "").strip()
                    if not path:
                        continue
                    send_files = getattr(chat, "SendFiles", None)
                    if not callable(send_files):
                        return False
                    result = self._run_private_outbound_echo_send(
                        target,
                        {"type": "file", "path": path},
                        lambda: send_files(filepath=path),
                        source=source,
                    )
                elif action_type in {"voice", "audio"}:
                    path = str((action or {}).get("path") or "").strip()
                    if not path:
                        continue
                    send_audio = getattr(chat, "SendAudio", None)
                    if not callable(send_audio):
                        return False
                    def send_voice():
                        try:
                            return send_audio(filepath=path, duration=None)
                        except TypeError:
                            return send_audio(path)

                    result = self._run_private_outbound_echo_send(
                        target,
                        {"type": "voice", "path": path},
                        send_voice,
                        source=source,
                    )
                else:
                    text = str((action or {}).get("text") or "").strip()
                    if not text:
                        continue
                    result = self._run_private_outbound_echo_send(
                        target,
                        {"type": "text", "text": text},
                        lambda: chat.SendMsg(text),
                        source=source,
                    )
        return result

    def _run_private_outbound_echo_send(self, target, action, sender, *, source):
        echo_group_id = self._reserve_private_outbound_echo_for_action(target, action, source=source)
        try:
            result = sender()
        except wechat_ui_actions.IntentCancelled:
            self._discard_private_outbound_echo_group(echo_group_id)
            raise
        except Exception:
            self._settle_private_outbound_echo_send(echo_group_id, None)
            raise
        self._settle_private_outbound_echo_send(echo_group_id, result)
        return result

    def _flush_lightweight_send_queue(self, *, limit=20):
        self._ensure_lightweight_send_queue_state()
        if wechat_ui_actions.is_busy(self):
            return False
        with self._lightweight_send_queue_lock:
            if self._lightweight_send_queue_flushing:
                return False
            self._lightweight_send_queue_flushing = True
        flushed = False
        try:
            for _ in range(max(1, int(limit or 1))):
                with self._lightweight_send_queue_lock:
                    if not self._lightweight_send_queue:
                        break
                    target, item = next(iter(self._lightweight_send_queue.items()))
                    expected_sequence = item.get("expected_sequence")
                    if expected_sequence is not None and self._get_private_message_sequence(target) != expected_sequence:
                        self._lightweight_send_queue.pop(target, None)
                        log(message=f"[轻量发送队列] {target} 已有新消息，丢弃上一轮过期回复")
                        continue
                release_wechat_lock = wechat_ui_actions.try_acquire(self)
                if not release_wechat_lock:
                    break
                try:
                    result = self._send_lightweight_actions_to_child(
                        target,
                        item.get("actions") or [],
                        source=str(item.get("source") or "lightweight_send"),
                        expected_sequence=expected_sequence,
                        delivery_id=str(item.get("delivery_id") or ""),
                    )
                except wechat_ui_actions.IntentCancelled as exc:
                    with self._lightweight_send_queue_lock:
                        if self._lightweight_send_queue.get(target) is item:
                            self._lightweight_send_queue.pop(target, None)
                    log(message=f"[轻量发送队列] {target} 排队任务已取消：{exc}")
                    continue
                except wechat_ui_actions.ActionBatchInterrupted as exc:
                    with self._lightweight_send_queue_lock:
                        if self._lightweight_send_queue.get(target) is item:
                            self._lightweight_send_queue.pop(target, None)
                    log(
                        level="WARNING",
                        message=f"[轻量发送队列] {target} 投递结果未知，已禁止自动重发：{exc}",
                    )
                    continue
                finally:
                    release_wechat_lock()
                if ReplyCountStore.was_send_success(result):
                    with self._lightweight_send_queue_lock:
                        current = self._lightweight_send_queue.get(target)
                        if current is item:
                            self._lightweight_send_queue.pop(target, None)
                    flushed = True
                    log(level="SUCCESS", message=f"[轻量发送队列] {target} 延后发送已完成")
                    continue
                log(level="WARNING", message=f"[轻量发送队列] {target} 待发送任务暂未发出，保留队列")
                break
            return flushed
        finally:
            with self._lightweight_send_queue_lock:
                self._lightweight_send_queue_flushing = False

    def _send_text_to_target_without_child(
        self,
        target,
        msg,
        *,
        contact_key="",
        task_key="",
        task_version=0,
        require_contact_key=False,
    ):
        target = str(target or "").strip()
        if not target:
            return False
        if getattr(self, "_ui_owner", None) is not None:
            text = str(msg or "")
            result = self._run_private_outbound_echo_send(
                target,
                {"type": "text", "text": text},
                lambda: self._ui_owner.call(
                    wechat_ui_actions.UIIntent(
                        wechat_ui_actions.UIIntentKind.SEND_TEXT,
                        {
                            "conversation": target,
                            "contact_key": str(contact_key or ""),
                            "task_key": str(task_key or ""),
                            "require_contact_key": bool(require_contact_key),
                            "text": text,
                        },
                        task_version=task_version,
                    ),
                    wechat_ui_actions.UI_CALL_WAIT_TIMEOUT,
                ),
                source="text",
            )
            return result
        release_wechat_lock = wechat_ui_actions.try_acquire(self)
        if not release_wechat_lock:
            return self._queue_lightweight_send(
                target,
                [{"type": "text", "text": str(msg or "")}],
                source="text",
            )
        try:
            chat = self._ensure_target_listen_chat_for_send(target)
            if not chat:
                return self._queue_lightweight_send(
                    target,
                    [{"type": "text", "text": str(msg or "")}],
                    source="text",
                )
            with self._get_chat_send_lock(target):
                result = self._run_private_outbound_echo_send(
                    target,
                    {"type": "text", "text": str(msg or "")},
                    lambda: chat.SendMsg(str(msg or "")),
                    source="text",
                )
                return result
        finally:
            release_wechat_lock()

    def _queue_text_reply_until_target_verified(self, target, parts, *, source="reply", expected_sequence=None):
        bubbles = []
        for part in parts or []:
            text = str(part or "")
            if not text.strip():
                continue
            if len(text) >= LONG_REPLY_SEGMENT_CHARS:
                bubbles.extend(
                    str(segment or "")
                    for segment in self.config.split_long_text(text)
                    if str(segment or "").strip()
                )
            else:
                bubbles.append(text)
        actions = [{"type": "text", "text": bubble} for bubble in bubbles]
        if not actions:
            return False
        return self._queue_lightweight_send(
            target,
            actions,
            source=source,
            expected_sequence=expected_sequence,
        )

    def _queue_keyword_reply_until_target_verified(self, target, actions):
        queued_actions = []
        for action in actions or []:
            action_type = str((action or {}).get("type") or "").strip().lower()
            if action_type == "text":
                content = str((action or {}).get("content") or "").strip()
                if content:
                    queued_actions.append({"type": "text", "text": content})
            else:
                path = str((action or {}).get("path") or "").strip()
                if path:
                    queued_actions.append({"type": "file", "path": path})
        if not queued_actions:
            return False
        return self._queue_lightweight_send(target, queued_actions, source="keyword_reply")

    def _send_file_to_target_without_child(
        self,
        target,
        path,
        *,
        contact_key="",
        task_key="",
        task_version=0,
        require_contact_key=False,
    ):
        target = str(target or "").strip()
        path = str(path or "").strip()
        if not target or not path:
            return False
        if getattr(self, "_ui_owner", None) is not None:
            result = self._run_private_outbound_echo_send(
                target,
                {"type": "file", "path": path},
                lambda: self._ui_owner.call(
                    wechat_ui_actions.UIIntent(
                        wechat_ui_actions.UIIntentKind.SEND_FILE,
                        {
                            "conversation": target,
                            "contact_key": str(contact_key or ""),
                            "task_key": str(task_key or ""),
                            "require_contact_key": bool(require_contact_key),
                            "path": path,
                        },
                        task_version=task_version,
                    ),
                    wechat_ui_actions.UI_CALL_WAIT_TIMEOUT,
                ),
                source="file",
            )
            return result
        release_wechat_lock = wechat_ui_actions.try_acquire(self)
        if not release_wechat_lock:
            return self._queue_lightweight_send(
                target,
                [{"type": "file", "path": path}],
                source="file",
            )
        try:
            chat = self._ensure_target_listen_chat_for_send(target)
            if not chat or not runtime_chat_state.listen_chat_has_method(chat, "SendFiles"):
                return self._queue_lightweight_send(
                    target,
                    [{"type": "file", "path": path}],
                    source="file",
                )
            with self._get_chat_send_lock(target):
                result = self._run_private_outbound_echo_send(
                    target,
                    {"type": "file", "path": path},
                    lambda: chat.SendFiles(filepath=path),
                    source="file",
                )
                return result
        finally:
            release_wechat_lock()

    def _send_actions_to_target_without_child(
        self,
        target,
        actions,
        *,
        contact_key="",
        task_key="",
        task_version=0,
        delivery_id="",
        require_contact_key=False,
    ):
        target = str(target or "").strip()
        actions = [dict(action or {}) for action in (actions or [])]
        if not target or not actions or getattr(self, "_ui_owner", None) is None:
            return False
        echo_group_ids = [
            self._reserve_private_outbound_echo_for_action(target, action, source="runtime_send")
            for action in actions
        ]
        try:
            results = OwnedChat(self._ui_owner, target).SendActions(
                actions,
                contact_key=contact_key,
                task_key=task_key,
                task_version=task_version,
                delivery_id=delivery_id,
                require_contact_key=require_contact_key,
            )
        except wechat_ui_actions.IntentCancelled:
            for echo_group_id in echo_group_ids:
                self._discard_private_outbound_echo_group(echo_group_id)
            raise
        except Exception:
            for echo_group_id in echo_group_ids:
                self._settle_private_outbound_echo_send(echo_group_id, None)
            raise
        results = list(results or []) if isinstance(results, (list, tuple)) else [results]
        for echo_group_id, result in zip(echo_group_ids, results):
            self._settle_private_outbound_echo_send(echo_group_id, result)
        return results

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
        try:
            ok, message = webhook_send.send_message(id, content)
            if not ok:
                log(level="ERROR", message=f"发送 Webhook 通知失败：{message}")
        except Exception as webhook_err:
            log(level="ERROR", message=f"发送 Webhook 通知异常：{webhook_err}")

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
            'moments_api_index',
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

    def _moments_task_storage(self, *, wx_id=None):
        data_dir = self._task_storage_data_dir()
        if not data_dir:
            return None
        return TaskWorkbenchStorage(data_dir, self._task_storage_wx_id(wx_id), "moments")

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

    def _save_moments_task_definitions_only(self, tasks):
        storage = self._moments_task_storage()
        if storage is None:
            return
        normalized = [
            normalize_moments_task(task)
            for task in (tasks or [])
            if isinstance(task, dict)
        ]
        definitions, _runtime_map, _history_map = serialize_moments_task_collection(normalized)
        storage.save_tasks(definitions)

    def _save_moments_runtime_record(self, task):
        storage = self._moments_task_storage()
        if storage is None:
            return
        definition, runtime_record, _history = split_moments_task_storage(task)
        task_id = str(definition.get("id") or "").strip()
        if not task_id:
            return
        storage.mutate_runtime(
            lambda runtime_map: {
                **(runtime_map if isinstance(runtime_map, dict) else {}),
                task_id: runtime_record,
            }
        )

    def _save_moments_runtime_history_records(self, task):
        storage = self._moments_task_storage()
        if storage is None:
            return
        definition, runtime_record, history_record = split_moments_task_storage(task)
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

    def _resolve_panel_moments_images(self, images):
        data_dir = str(getattr(self.config, "DATA_DIR", "") or "")
        resolved = []
        for image in images or []:
            text = str(image or "").strip()
            if not text:
                continue
            if os.path.isabs(text):
                resolved.append(text)
            else:
                resolved.append(os.path.abspath(os.path.join(data_dir, text)))
        return resolved

    def _panel_moments_privacy(self, visibility_type):
        return moments_visibility_to_privacy(visibility_type)

    def _run_due_moments_task_list(self, now=None):
        runtime_task_runner.run_due_moments_task_list(self, now=now)

    def _process_unified_runtime_tasks(self, now=None):
        runtime_task_runner.process_unified_runtime_tasks(self, now=now)

    def _process_pending_runtime_task_reload(self):
        return runtime_task_runner.process_pending_runtime_task_reload(self)

    def _listen_add_error(self, result):
        return listening.listen_add_error(result)

    def _subwindow_who(self, chat):
        return listening.subwindow_who(chat)

    def _get_verified_subwindow(self, nickname):
        return listening.get_verified_subwindow(self, nickname)

    def _try_get_all_subwindow_names(self):
        return listening.try_get_all_subwindow_names(self)

    def _add_listen_chat_once(self, nickname, label):
        return listening.add_listen_chat_once(self, nickname, label)

    def _add_and_verify_subwindow(self, nickname, retry_count=3):
        return listening.add_and_verify_subwindow(self, nickname, retry_count=retry_count)

    def _expected_listener_names(self):
        return listening.expected_listener_names(self)

    def _ensure_listener_subwindow(self, nickname, retry_count=3):
        return listening.ensure_listener_subwindow(self, nickname, retry_count=retry_count)

    def _reconcile_listener_subwindows(self, retry_count=3):
        return listening.reconcile_listener_subwindows(self, retry_count=retry_count)

    def _maybe_reconcile_listener_subwindows(self, force=False, retry_count=3):
        return listening.maybe_reconcile_listener_subwindows(self, force=force, retry_count=retry_count)

    def _remove_listen_chat_verified(self, nickname, *, log_success=True):
        return listening.remove_listen_chat_verified(self, nickname, log_success=log_success)

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

    # ----------------------------------------------------------
    # 定时消息发送
    # ----------------------------------------------------------

    def send_scheduled_msg(self, targets, msgs, repeat_type, weekdays, dates, task_id):
        """直接执行一个已到期的定时消息任务；运行时调度统一由 scheduled_message_task_list 负责。"""
        result = execute_scheduled_message_task(
            task={
                "id": task_id,
                "targets": list(targets or []),
                "msgs": list(msgs or []),
                "repeat_mode": "once" if repeat_type == "once" else "repeat",
            },
            send_text=lambda target, msg: runtime_chat_state.send_text_to_target(self, target, msg),
            send_file=lambda target, path: runtime_chat_state.send_file_to_target(self, target, path),
            is_image_path=self.is_image_path,
            human_delay=self._inter_message_delay_or_stop,
            should_stop=self.is_stop_requested,
            notify_error=self.is_err,
            nickname=self.wx.nickname,
            scheduled_tasks=[],
            config_data={},
            save_config=None,
            log_info=lambda message: log(message=message),
            log_error=lambda message: log(level="WARNING", message=message),
        )
        success_count = int((result or {}).get("success_count", 0) or 0) if isinstance(result, dict) else 0
        queued_count = int((result or {}).get("queued_count", 0) or 0) if isinstance(result, dict) else 0
        result_type = str((result or {}).get("result_type", "") or "") if isinstance(result, dict) else ""
        if success_count > 0:
            level = "SUCCESS"
        elif result_type == "queued" and queued_count > 0:
            level = "INFO"
        else:
            level = "WARNING"
        log(
            level=level,
            message=f"定时消息任务 {task_id or '未命名任务'} 完成：{runtime_task_runner._scheduled_message_result_summary(result)}",
        )
        return result

    def _execute_moments_publish_task(self, task):
        owner = getattr(self, "_ui_owner", None)
        if owner is None:
            raise RuntimeError("朋友圈 UI 只能由微信 UI owner 执行")
        task = dict(task or {})
        task_key = str(task.pop("_ui_task_key", "") or "")
        task_version = int(task.pop("_ui_task_version", 0) or 0)
        try:
            return owner.call(
                wechat_ui_actions.UIIntent(
                    wechat_ui_actions.UIIntentKind.MOMENTS,
                    {
                        "task": task,
                        "task_key": task_key,
                        "nickname": str(self._ui_identity.get("nickname") or ""),
                    },
                    task_version=task_version,
                ),
                wechat_ui_actions.UI_CALL_WAIT_TIMEOUT,
            )
        except wechat_ui_actions.IntentCancelled as exc:
            log(level="INFO", message=f"朋友圈任务已更新或取消，本次未发布：{exc}")
            return False

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
        log(
            level="SUCCESS" if summary["success"] > 0 else "WARNING",
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
        success = False
        for attempt in range(3):
            send_records = self._load_material_send_records()
            send_count_before = len(send_records)
            result = self._attempt_material_outreach_batches(task, send_records, allow_rebuild=True)
            if (
                self._material_outreach_is_deferred(result)
                or self._material_outreach_is_stopped(result)
                or self._material_outreach_preface_is_queued(result)
            ):
                return result
            if result:
                success = True
                break
            send_count_after = len(self._load_material_send_records())
            if send_count_after <= send_count_before:
                break
            if attempt == 0:
                log(message="[素材转发] 运行时素材发送失败，准备重建素材池重试")
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

    def _has_pending_lightweight_send_queue(self):
        return contacts.has_pending_lightweight_send_queue(self)

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
        block_on_wechat_lock=True,
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
            block_on_wechat_lock=block_on_wechat_lock,
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
        block_on_wechat_lock=True,
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
            block_on_wechat_lock=block_on_wechat_lock,
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
                    result = self._send_material_outreach_action_with_batch_lock(task, action, materials)
            else:
                result = self._send_material_outreach_action_with_batch_lock(task, action, materials)
            if self._material_ui_task_is_stale():
                return {"status": "cancelled", "message": "素材转发任务已更新或取消"}
            if self._material_outreach_is_deferred(result):
                return result
            if self._material_outreach_is_uncertain(result):
                return result
            self._flush_lightweight_send_queue()
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

    def _send_material_outreach_action_with_batch_lock(self, task, action, materials):
        release_wechat_lock = wechat_ui_actions.try_acquire(self)
        if not release_wechat_lock:
            return self._material_outreach_lock_busy_result()
        try:
            return self._send_material_outreach_action(task, action, materials)
        finally:
            release_wechat_lock()

    def _material_outreach_lock_busy_result(self):
        log(message="[素材转发] 微信 UI 正忙，本批次稍后重试")
        return {
            "status": "deferred_lock_busy",
            "reason": "wechat_ui_busy",
            "message": "微信 UI 正忙，素材转发批次稍后重试",
        }

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

    def _read_material_source_messages(self, source, limit, *, goback=True, target_signature="", require_forwardable=True):
        source = str(source or "").strip()
        limit = max(1, int(limit or 1))
        if getattr(self, "_ui_owner", None) is not None:
            result = self._ui_owner.call(
                wechat_ui_actions.UIIntent(
                    wechat_ui_actions.UIIntentKind.MATERIAL_READ,
                    {
                        "conversation": source,
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
        last_messages = None

        def normalize_messages(messages):
            return list(messages or [])

        def messages_are_usable(messages):
            if not require_forwardable:
                return True
            return any(is_forwardable_material_message(message) for message in messages or [])

        def remember_unusable_messages(messages, strategy):
            nonlocal last_messages
            last_messages = messages
            log(
                level="WARNING",
                message=(
                    f"[素材转发] 读取素材历史未发现可转发素材，准备尝试下一读取方案："
                    f"来源 {source}，方案 {strategy}，读取 {len(messages)} 条"
                ),
            )

        with self._get_material_source_read_lock(source):
            with wechat_ui_actions.hold(self):
                source_chat = self._ensure_material_source_chat(source)
                for source_reader, source_strategy in self._material_history_readers(
                    source_chat,
                    window_label="子窗口",
                    prefer_internal=True,
                ):
                    try:
                        with warn_slow_wechat_ui_action(f"{source_strategy}({source}, n={limit})"):
                            messages = self._get_material_history_messages(
                                source_reader,
                                limit,
                                goback=goback,
                                target_signature=target_signature,
                            )
                        messages = normalize_messages(messages)
                        if messages_are_usable(messages):
                            self._set_material_source_read_strategy(source, source_strategy)
                            return messages
                        remember_unusable_messages(messages, source_strategy)
                    except Exception as exc:
                        log(
                            level="WARNING",
                            message=f"[素材转发] 子窗口读取素材历史失败，准备尝试下一读取方案：来源 {source}，方案 {source_strategy}，{exc}",
                        )
                if callable(getattr(self.wx, "GetHistoryMessage", None)) and callable(getattr(self.wx, "ChatWith", None)):
                    try:
                        with warn_slow_wechat_ui_action(f"ChatWith({source})"):
                            self.wx.ChatWith(source, exact=True)
                        main_reader, main_strategy = self._material_history_reader(
                            self.wx,
                            window_label="主窗口",
                            allow_internal=False,
                        )
                        with warn_slow_wechat_ui_action(f"{main_strategy}({source}, n={limit})"):
                            messages = self._get_material_history_messages(
                                main_reader,
                                limit,
                                goback=goback,
                                target_signature=target_signature,
                            )
                        messages = normalize_messages(messages)
                        if messages_are_usable(messages):
                            self._set_material_source_read_strategy(source, main_strategy)
                            return messages
                        remember_unusable_messages(messages, main_strategy)
                    except Exception as exc:
                        log(
                            level="WARNING",
                            message=f"[素材转发] 主窗口读取素材历史失败，准备尝试子窗口可见消息兜底：来源 {source}，{exc}",
                        )
                if callable(getattr(source_chat, "GetAllMessage", None)):
                    with warn_slow_wechat_ui_action(f"子窗口可见 GetAllMessage({source}, n={limit})"):
                        visible_strategy = "子窗口可见 GetAllMessage"
                        messages = normalize_messages(self._get_material_visible_messages(source_chat, limit))
                        if messages_are_usable(messages):
                            self._set_material_source_read_strategy(source, visible_strategy)
                            return messages
                        remember_unusable_messages(messages, visible_strategy)
                if last_messages is not None:
                    self._set_material_source_read_strategy(source, "未读取到可转发素材")
                    return last_messages
                raise RuntimeError("素材来源窗口不支持读取消息")

    def _material_history_readers(self, chat, *, window_label, prefer_internal=False, allow_internal=True):
        if not chat or isinstance(chat, dict):
            return []
        readers = []
        chat_box = getattr(chat, "ChatBox", None)
        get_internal_history = getattr(chat_box, "get_msgs_from_history", None)
        get_history = getattr(chat, "GetHistoryMessage", None)
        internal_reader = (
            (get_internal_history, f"{window_label}内部 ChatBox.get_msgs_from_history")
            if allow_internal and callable(get_internal_history)
            else None
        )
        public_reader = (
            (get_history, f"{window_label}公开 GetHistoryMessage")
            if callable(get_history)
            else None
        )
        ordered = (internal_reader, public_reader) if prefer_internal else (public_reader, internal_reader)
        for item in ordered:
            if item is not None:
                readers.append(item)
        return readers

    def _material_history_reader(self, chat, *, window_label, prefer_internal=False, allow_internal=True):
        readers = self._material_history_readers(
            chat,
            window_label=window_label,
            prefer_internal=prefer_internal,
            allow_internal=allow_internal,
        )
        if not readers:
            return None, ""
        return readers[0]

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

    def _get_material_history_messages(self, get_history, limit, *, goback=True, target_signature=""):
        limit = max(1, int(limit or 1))
        target_signature = str(target_signature or "").strip()
        forwardable_seen = 0
        stop_sign = getattr(WxParam, "CALLBACK_STOP_SIGN", "stop")

        def stop_after_enough_materials(message):
            nonlocal forwardable_seen
            if target_signature and build_stable_material_signature(message) == target_signature:
                return stop_sign
            if is_forwardable_material_message(message):
                forwardable_seen += 1
            if forwardable_seen >= limit:
                return stop_sign

        return get_history(
            limit,
            callback=stop_after_enough_materials,
            interval=0.2,
            speed=5,
            goback=goback,
        ) or []

    def _get_material_visible_messages(self, source_chat, limit):
        get_all = getattr(source_chat, "GetAllMessage", None)
        if callable(get_all):
            messages = list(get_all() or [])
            if len(messages) > limit:
                return messages[-limit:]
            return messages
        raise RuntimeError("素材来源子窗口不支持读取可见消息")

    def _material_source_chat_is_usable(self, chat):
        return bool(
            chat
            and not isinstance(chat, dict)
            and (
                callable(getattr(chat, "GetHistoryMessage", None))
                or bool(self._material_history_reader(chat, window_label="子窗口", prefer_internal=True)[0])
                or callable(getattr(chat, "GetAllMessage", None))
            )
        )

    def _ensure_material_source_chat(self, source):
        source = str(source or "").strip()
        if not source:
            raise RuntimeError("素材来源为空")
        if not hasattr(self, "_material_source_chats") or self._material_source_chats is None:
            self._material_source_chats = {}
        cached = self._material_source_chats.get(source)
        if self._material_source_chat_is_usable(cached):
            return cached
        listened = runtime_chat_state.get_listen_chat(self, source)
        if self._material_source_chat_is_usable(listened):
            self._material_source_chats[source] = listened
            return listened
        get_subwindow = getattr(self.wx, "GetSubWindow", None)
        if callable(get_subwindow):
            try:
                subwindow = get_subwindow(source)
                if self._material_source_chat_is_usable(subwindow):
                    runtime_chat_state.remember_listen_chat(self, source, subwindow)
                    self._material_source_chats[source] = subwindow
                    return subwindow
            except Exception:
                pass
        with wechat_ui_actions.hold(self):
            cached = self._material_source_chats.get(source)
            if self._material_source_chat_is_usable(cached):
                return cached

            def add_material_source_chat():
                with warn_slow_wechat_ui_action(f"AddListenChat({source})"):
                    return self.wx.AddListenChat(nickname=source, callback=self.message_handle_callback)

            result = run_with_wechat_rebind_retry(
                self,
                add_material_source_chat,
                attempts=2,
                on_retry=lambda exc, _attempt: log(
                    level="WARNING",
                    message=f"[素材转发] 恢复素材来源子窗口异常，重新初始化微信客户端后重试：{source}，{exc}",
                ),
            )
            if not self._material_source_chat_is_usable(result):
                raise RuntimeError(f"素材来源子窗口恢复失败：{source}，{result}")
            runtime_chat_state.remember_listen_chat(self, source, result)
            self._material_source_chats[source] = result
            log(message=f"[素材转发] 已恢复素材来源子窗口：{source}")
            return result

    def _get_material_source_messages(self, source_chat, limit, *, goback=True, target_signature=""):
        get_history, _strategy = self._material_history_reader(
            source_chat,
            window_label="子窗口",
            prefer_internal=True,
        )
        if callable(get_history):
            return self._get_material_history_messages(
                get_history,
                limit,
                goback=goback,
                target_signature=target_signature,
            )
        if callable(getattr(source_chat, "GetAllMessage", None)):
            return self._get_material_visible_messages(source_chat, limit)
        raise RuntimeError("素材来源子窗口不支持读取消息")

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
                )
        return self._forward_material_message_unlocked(
            message,
            targets,
            preface=preface,
            material_source=material_source,
            material_type=material_type,
            material_title=material_title,
            echo_source=echo_source,
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
    ):
        with wechat_ui_actions.hold(self):
            target_label = "、".join(str(item or "").strip() for item in (targets or []) if str(item or "").strip())
            echo_group_id = self._remember_material_outbound_echoes(
                targets,
                material_type or getattr(message, "type", ""),
                preface=preface,
                material_title=material_title or getattr(message, "content", ""),
                source=echo_source,
                schedule_fallback=False,
            )
            try:
                with warn_slow_wechat_ui_action(f"message.forward({target_label or 'unknown'})"):
                    if getattr(self, "_ui_owner", None) is not None:
                        try:
                            result = self._ui_forward_message(
                                OwnedChat(self._ui_owner, material_source),
                                message,
                                targets,
                                preface=preface,
                            )
                        except wechat_ui_actions.IntentCancelled:
                            self._discard_private_outbound_echo_group(echo_group_id)
                            return False, "素材转发任务已更新或取消"
                    else:
                        roll_into_view = getattr(message, "roll_into_view", None)
                        if callable(roll_into_view):
                            roll_into_view()
                        if not callable(getattr(message, "forward", None)):
                            raise RuntimeError("素材消息不可转发")
                        result = message.forward(targets, message=preface) if preface else message.forward(targets)
            except Exception:
                self._schedule_private_outbound_echo_fallback("")
                raise
            success, result_error = is_forward_result_success(result)
            if success:
                self._schedule_private_outbound_echo_fallback("")
            else:
                self._mark_private_outbound_echo_group_reported_failed(echo_group_id)
            return success, result_error

    def _remember_material_outbound_echoes(
        self,
        targets,
        material_type="",
        *,
        preface="",
        material_title="",
        source="material_outreach",
        schedule_fallback=True,
    ):
        kind = str(material_type or "").strip().lower() or "unknown"
        preface = str(preface or "").strip()
        material_title = str(material_title or "").strip()
        echo_group_id = uuid.uuid4().hex
        for target in targets or []:
            target = str(target or "").strip()
            if not target:
                continue
            self._remember_private_outbound_echo(
                target,
                kind,
                source=source,
                echo_group_id=echo_group_id,
                content=material_title,
                fallback_content=material_title or "[素材]",
            )
            if preface:
                self._remember_private_outbound_echo(
                    target,
                    "text",
                    preface,
                    source=source,
                    echo_group_id=echo_group_id,
                    fallback_content=preface,
                )
        if schedule_fallback:
            self._schedule_private_outbound_echo_fallback("")
        return echo_group_id

    def _discard_private_outbound_echo_group(self, echo_group_id):
        group_id = str(echo_group_id or "").strip()
        if not group_id:
            return False
        kept_reserved = False
        with self._chat_merge_lock:
            next_echoes = {}
            for name, echoes in self._private_outbound_echoes.items():
                kept = []
                for item in echoes or []:
                    if not isinstance(item, dict) or item.get("echo_group_id") != group_id:
                        kept.append(item)
                        continue
                    if item.get("reservation_id"):
                        kept.append(item)
                        kept_reserved = True
                if kept:
                    next_echoes[name] = kept
            self._private_outbound_echoes = next_echoes
        if kept_reserved:
            self._schedule_private_outbound_echo_fallback("")
        return True

    def _mark_private_outbound_echo_group_reported_failed(self, echo_group_id):
        group_id = str(echo_group_id or "").strip()
        if not group_id:
            return False
        now = time.time()
        changed = False
        with self._chat_merge_lock:
            for echoes in self._private_outbound_echoes.values():
                for item in echoes or []:
                    if not isinstance(item, dict) or item.get("echo_group_id") != group_id:
                        continue
                    item["reported_failed"] = True
                    item["fallback_content"] = ""
                    item["primary_memory_writer"] = False
                    item["expires_at"] = now + PRIVATE_OUTBOUND_REPORTED_FAILED_CALLBACK_SECONDS
                    changed = True
        return changed

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
        batch_id = f"batch_{uuid.uuid4().hex}"
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
            )
            if not success and self._material_forward_error_needs_refresh(error):
                log(level="WARNING", message=f"[素材转发] 素材句柄失效，已刷新来源子窗口后重试：{title}")
                material, message, materials = self._refresh_material_runtime_message(material, materials)
                material_id = material.get("id")
                title = material_title(material)
                material_type = material.get("type_bucket") or material.get("type")
                material_source = material.get("source", "")
                if message is not None:
                    success, error = self._forward_material_message(
                        message,
                        targets,
                        preface=preface,
                        material_source=material_source,
                        material_type=material_type,
                        material_title=title,
                    )
        except Exception as exc:
            error = str(exc)
            if self._material_forward_error_needs_refresh(error):
                try:
                    log(level="WARNING", message=f"[素材转发] 素材句柄异常失效，已刷新来源子窗口后重试：{title}")
                    material, message, materials = self._refresh_material_runtime_message(material, materials)
                    material_id = material.get("id")
                    title = material_title(material)
                    material_type = material.get("type_bucket") or material.get("type")
                    material_source = material.get("source", "")
                    if message is not None:
                        success, error = self._forward_material_message(
                            message,
                            targets,
                            preface=preface,
                            material_source=material_source,
                            material_type=material_type,
                            material_title=title,
                        )
                except Exception as retry_exc:
                    error = str(retry_exc)
                    uncertain = not self._material_forward_error_needs_refresh(error)
            else:
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
            level="SUCCESS" if success else "WARNING",
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
        if getattr(msg, "_wxbot_unanswered_recovery", False):
            return self.process_message(chat, msg)
        try:
            received_at = datetime.now()
            self._last_incoming_message_at = time.time()
            setattr(msg, "_wxbot_ingress_source", "subwindow")
            setattr(msg, "_wxbot_received_at", received_at)
            if (
                self._is_ordinary_private_self_message(chat, msg)
                and not bool(getattr(msg, "_wxbot_seen_at_ingress", False))
            ):
                if not self._mark_message_seen(chat.who, msg):
                    return True
                setattr(msg, "_wxbot_seen_at_ingress", True)
            message_routing.record_runtime_inbound_event(
                self,
                msg,
                getattr(chat, "chat_type", ""),
            )
            if takeover_runtime.consume_admin_chat_echo_message(self, chat, msg):
                self._mark_message_skip_memory(msg)
                self._consume_private_reply_runtime_echo(chat.who, getattr(msg, "content", ""))
                return True
            msg_type_label = {
                "text": "文本",
                "voice": "语音",
                "image": "图片",
                "video": "视频",
                "file": "文件",
            }.get(str(getattr(msg, "type", "") or "").lower(), str(getattr(msg, "type", "") or "未知"))
            is_private = getattr(msg, "attr", "") == "friend"
            scene_label = "私聊" if is_private else "消息"
            text = f"{scene_label} {chat.who}：收到{msg_type_label}消息"
            if not is_private:
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

            if msg.attr == "friend":
                callback_result = message_routing.handle_friend_message_callback(self, msg, chat, text=text)

            elif msg.attr == "system":
                # 系统消息：触发群新人欢迎语逻辑（仅限已配置群组，纯转发来源群组跳过）
                if self.config.group_welcome and chat.who in self.config.group:
                    result = self.send_group_welcome_msg(chat, msg)
                    if not result:
                        self.is_err(
                            self.wx.nickname + f" wxbot发送群新人欢迎语失败！",
                            text + '\n' + self._result_error_text(result),
                        )

            elif msg.attr == "self":
                # 自己账号同步过来的消息（如从手机向文件传输助手发送指令）
                # 仅当当前窗口与管理员配置匹配时才作为指令处理
                if chat.who == self.config.cmd:
                    self._record_received_message()
                    self.last_msg_time   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self.last_msg_sender = msg.sender
                    with takeover_runtime.capture_admin_chat_replies(self, chat):
                        admin_chat = chat
                        if self._handle_admin_forward_input(admin_chat, msg):
                            return True
                        if self._handle_admin_moments_input(admin_chat, msg):
                            return True
                        result = self.process_command(admin_chat, msg)
                        if not result:
                            self.is_err(
                                self.wx.nickname + f" wxbot处理管理员指令失败！",
                                text + '\n' + self._result_error_text(result),
                            )
                        return True
                if self._handle_material_source_message(chat, msg):
                    return True

            ordinary_private_self = self._is_ordinary_private_self_message(chat, msg)
            self._save_incoming_memory_message(chat, msg)
            if ordinary_private_self and not bool(getattr(msg, "_wxbot_private_outbound_echo", False)):
                runtime_echo = self._consume_private_reply_runtime_echo(chat.who, getattr(msg, "content", ""))
                if not runtime_echo:
                    self._handle_private_self_message_boundary(chat, msg)
            if callback_result is not None:
                return callback_result
        except Exception as e:
            # 回调函数出现未捕获异常时标记 callback_is_die，由主循环检测并处理
            if self._arm_listener_auto_recovery(e, source="消息回调"):
                return
            self.callback_is_die = True
            self.is_err(self.wx.nickname + " wxbot回调函数处理出错！处理监听失败！！", e)

    @staticmethod
    def _mark_message_skip_memory(message):
        try:
            setattr(message, "_skip_memory", True)
        except Exception:
            pass
        return message

    def _is_ordinary_private_self_message(self, chat, message):
        return (
            getattr(message, "attr", "") == "self"
            and getattr(chat, "who", "") != getattr(self.config, "cmd", "")
            and getattr(chat, "chat_type", "private") != "group"
        )

    def _should_skip_message_memory(self, chat, message):
        if bool(getattr(message, "_skip_memory", False)):
            return True
        if bool(getattr(message, "_wxbot_private_reply_persisted_echo", False)):
            return True
        if bool(getattr(message, "_wxbot_private_outbound_echo_duplicate", False)):
            if not bool(getattr(message, "_wxbot_private_outbound_echo_primary", False)):
                return True
            if self._private_outbound_echo_primary_reservation_active(message):
                return True
        if bool(getattr(message, "_wxbot_private_outbound_echo", False)):
            return False
        if (
            self._is_ordinary_private_self_message(chat, message)
        ):
            outbound_echo = self._consume_private_outbound_echo(
                chat.who,
                message=message,
                return_match=True,
            )
            if outbound_echo:
                try:
                    setattr(message, "_wxbot_private_outbound_echo", True)
                    if outbound_echo.get("reservation_id"):
                        setattr(message, "_wxbot_outbound_echo_reservation", outbound_echo["reservation_id"])
                    if (
                        outbound_echo.get("duplicate_reservation")
                        or outbound_echo.get("dedupe_only")
                        or outbound_echo.get("primary_memory_writer")
                    ):
                        setattr(message, "_wxbot_private_outbound_echo_duplicate", True)
                    if outbound_echo.get("primary_memory_writer"):
                        setattr(message, "_wxbot_private_outbound_echo_primary", True)
                except Exception:
                    pass
                if outbound_echo.get("memory_persisted"):
                    try:
                        setattr(message, "_wxbot_private_reply_persisted_echo", True)
                    except Exception:
                        pass
                    return True
                if (
                    outbound_echo.get("duplicate_reservation")
                    or outbound_echo.get("dedupe_only")
                    or outbound_echo.get("primary_memory_writer")
                ):
                    return True
        if (
            getattr(message, "type", "") == "voice"
            and message_routing.voice_content_state(getattr(message, "content", "")) != "valid"
        ):
            return True
        if (
            getattr(message, "attr", "") == "self"
            and getattr(chat, "who", "") == getattr(self.config, "cmd", "")
        ):
            return True
        return False

    def _drop_private_ai_lightweight_send_queue(self, chat_name):
        name = str(chat_name or "").strip()
        if not name:
            return False
        self._ensure_lightweight_send_queue_state()
        with self._lightweight_send_queue_lock:
            item = self._lightweight_send_queue.get(name)
            if not isinstance(item, dict):
                return False
            if str(item.get("source") or "").strip() != "private_ai_reply":
                return False
            self._lightweight_send_queue.pop(name, None)
            return True

    def _has_private_ai_lightweight_send_queue(self, chat_name):
        name = str(chat_name or "").strip()
        if not name:
            return False
        self._ensure_lightweight_send_queue_state()
        with self._lightweight_send_queue_lock:
            item = self._lightweight_send_queue.get(name)
            return (
                isinstance(item, dict)
                and str(item.get("source") or "").strip() == "private_ai_reply"
            )

    def _invalidate_private_ai_reply_turn(self, chat_name):
        name = str(chat_name or "").strip()
        if not name:
            return {}
        sequence = self._next_private_message_sequence(name)
        self._clear_private_message_pipeline(name)
        with self._chat_merge_lock:
            cancelled_voice = self._cancel_pending_private_voice_transcription_locked(name)
        dropped_queue = self._drop_private_ai_lightweight_send_queue(name)
        return {
            "sequence": sequence,
            "cancelled_voice_transcription": cancelled_voice,
            "dropped_lightweight_queue": dropped_queue,
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
        if has_committed_batch or self._has_private_ai_lightweight_send_queue(name):
            result = self._invalidate_private_ai_reply_turn(name)
            try:
                log(message=f"运行事件：人工介入已确认 runtime_id={self._runtime_instance_id}")
            except Exception:
                pass
            dropped_queue = bool(result.get("dropped_lightweight_queue"))
            suffix = "，已清理延迟发送队列" if dropped_queue else ""
            log(message=f"私聊 {name}：检测到 self 介入，已停止当前 AI 回复{suffix}")
            return True

        if has_open_batch or has_voice_transcription:
            self._invalidate_private_ai_reply_turn(name)
            try:
                log(message=f"运行事件：人工介入已确认 runtime_id={self._runtime_instance_id}")
            except Exception:
                pass
            log(message=f"私聊 {name}：检测到 self 边界，已切分待回复批次")
        else:
            log(message=f"私聊 {name}：检测到 self 消息，当前无待回复任务，已作为历史记录保留")
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
        if target and runtime_chat_state.is_single_chat_reply_paused(self, target):
            log(message=f"{log_prefix} {target}：当前好友已接管或暂停，已停止 AI 回复")
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

    def wx_send_ai(self, chat, message):
        store = getattr(self, "_unanswered_inbound_store", None)
        if store is None:
            return self._wx_send_ai_once(chat, message)
        context = getattr(self, "_unanswered_inbound_context", None)
        if context is None:
            context = threading.local()
            self._unanswered_inbound_context = context
        recovery_record_id = str(getattr(message, "_wxbot_recovery_record_id", "") or "")
        if recovery_record_id:
            record_id = recovery_record_id
            store.set_status(record_id, "replaying")
        else:
            record_id = store.begin(getattr(chat, "who", ""), message, chat_type="private")
        context.record_id = record_id
        try:
            result = self._wx_send_ai_once(chat, message)
        except BaseException:
            raise
        else:
            store.resolve(record_id)
            return result
        finally:
            if getattr(context, "record_id", "") == record_id:
                context.record_id = ""

    def _wx_send_ai_once(self, chat, message):
        """私聊 AI 自动回复。连续消息按好友串行处理，安全状态变化会停止发送。"""
        if not self._private_reply_can_continue(chat):
            return True
        reply_message_sequence = self._get_private_message_sequence(chat.who)
        result = True
        user_key = self._get_reply_count_key(chat, message)
        limit_handled, limit_result = self._check_text_reply_limit_runtime(chat, user_key, message=message)
        if limit_handled:
            return limit_result

        api_error_reply = False
        api_error_should_mark = False
        preprocess_fallback_should_mark = False
        voice_candidate = False
        start_voice_session = False
        image_reply_context_used = False
        voice_session_manager = VoiceSessionManager(
            getattr(self, "_voice_reply_state", None) or load_voice_reply_state(self._voice_reply_state_path())
        )
        self._voice_reply_state = voice_session_manager.state
        voice_now = datetime.now()
        try:
            message_type = str(getattr(message, "type", "") or "").strip().lower()
            message_body = strip_message_shell(getattr(message, "content", ""), message_type)
            message_semantic_text = format_message_semantic_text(message)
            model_message_text = format_model_message_text(message)
            keyword_plan = plan_private_keyword_reply(
                bool(getattr(self.config, "chat_keyword_switch", False)),
                self.config.keyword_dict,
                message_body,
            )
            if keyword_plan:
                log(message=f"私聊 {chat.who} 关键字消息：" + message_body)
                reply_actions = normalize_keyword_reply_actions(keyword_plan["reply"])
                send_success, result = self._send_keyword_reply_actions(
                    chat,
                    reply_actions,
                    delivery_id=self._stable_inbound_delivery_id("private-keyword", chat, message),
                )
                if send_success and self.config.text_reply_limit_switch and user_key:
                    self.reply_count_store.increment_ai_count(user_key)
                if send_success:
                    self._record_reply_metric_success(chat.who, chat_type="private")
                    self._record_keyword_reply_success(chat.who, chat_type="private", action_count=len(reply_actions))
                return result
            else:
                store = getattr(self, "_unanswered_inbound_store", None)
                context = getattr(self, "_unanswered_inbound_context", None)
                record_id = str(getattr(context, "record_id", "") or "") if context is not None else ""
                if store is not None and record_id:
                    store.set_status(record_id, "ai_started")
                history = []
                if self.config.memory_switch and self.config.memory_context_switch and self.memory_manager:
                    self._repair_private_context_before_ai(chat, message)
                    history = self._get_model_context_history(str(getattr(chat, "who", "") or "").strip())
                voice_candidate, start_voice_session = private_voice_candidate(
                    self.config,
                    chat.who,
                    message,
                    voice_session_manager,
                    now=voice_now,
                )
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
                    self._set_pending_visual_context(chat.who, [message.content])
                    reply = self._reply_private_image_message(
                        chat, history, [message.content]
                    )
                    image_reply_context_used = True
                elif self.config.chat_image_recognition_switch and quoted_image_paths:
                    self._set_pending_visual_context(chat.who, quoted_image_paths)
                    reply = self._reply_private_image_message(
                        chat, history, quoted_image_paths, quoted_text
                    )
                    image_reply_context_used = True
                elif fallback_image_path:
                    self._set_pending_visual_context(chat.who, [fallback_image_path])
                    reply = self._reply_private_image_message(
                        chat, history, [fallback_image_path]
                    )
                    image_reply_context_used = True
                elif self.config.chat_image_recognition_switch:
                    pending_visual_context = self._get_pending_visual_context(chat.who)
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
                section_id = ""
                if start_voice_session:
                    section_id = str(uuid.uuid4())
                elif voice_session_manager.is_private_session_active(chat.who, now=voice_now):
                    section_id = voice_session_manager.get_private_session_section_id(chat.who)
                    if not section_id:
                        section_id = str(uuid.uuid4())
                context_text = self._private_voice_context_text(message)
                if self._try_send_voice_reply(
                    chat,
                    clean_reply,
                    state_key=f"private:{chat.who}",
                    cooldown_minutes=getattr(self.config, 'chat_voice_reply_cooldown_minutes', 10) if start_voice_session else 0,
                    limit_count=getattr(self.config, 'chat_voice_reply_limit_count', 50),
                    limit_hours=getattr(self.config, 'chat_voice_reply_limit_hours', 24),
                    context_text=context_text,
                    section_id=section_id,
                    expected_sequence=reply_message_sequence,
                ):
                    if image_reply_context_used:
                        self._clear_pending_visual_context(chat.who)
                    if start_voice_session:
                        voice_session_manager.start_private_session(
                            chat.who,
                            now=voice_now,
                            minutes=getattr(self.config, 'chat_voice_session_minutes', 10),
                            turns=getattr(self.config, 'chat_voice_session_turns', 5),
                            section_id=section_id,
                        )
                    elif section_id:
                        voice_session_manager.set_private_session_section_id(chat.who, section_id)
                    if voice_session_manager.is_private_session_active(chat.who, now=voice_now):
                        voice_session_manager.consume_private_turn(chat.who)
                    self._save_voice_reply_state()
                    if self.config.text_reply_limit_switch and user_key:
                        self.reply_count_store.increment_ai_count(user_key)
                    self._record_reply_metric_success(chat.who, chat_type="private")
                    log(level="SUCCESS", message=f"私聊 {chat.who}：本轮回复已发送")
                    return True

        send_success, result = self._send_private_ai_reply_parts(
            chat,
            parts,
            expected_sequence=None if api_error_reply else reply_message_sequence,
        )
        if send_success:
            log(level="SUCCESS", message=f"私聊 {chat.who}：本轮回复已发送")

        if image_reply_context_used and send_success and not api_error_reply:
            if self._pending_visual_context_ready_to_clear(chat.who):
                self._clear_pending_visual_context(chat.who)
            else:
                log(message=f"私聊 {chat.who}：图片摘要尚未回写，暂保留最近图片上下文")

        if send_success and api_error_should_mark:
            self.reply_count_store.mark_api_err_notified(user_key)

        if send_success and preprocess_fallback_should_mark:
            self.reply_count_store.mark_preprocess_fallback_notified(user_key)

        if send_success and self.config.text_reply_limit_switch and user_key and not api_error_reply:
            self.reply_count_store.increment_ai_count(user_key)

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
        chat_type = getattr(chat, 'chat_type', 'private')
        if chat_type == 'group' or chat.who in getattr(self.config, 'group', []):
            return None
        system = getattr(self, 'prompt_system', None)
        if system is None:
            system = self._init_prompt_system()
        if not system.auto_memory_enabled_for(chat.who, chat_type='private'):
            return None
        try:
            messages = self.memory_manager.get_messages(
                str(getattr(chat, "who", "") or "").strip(),
                getattr(self.config, 'memory_max_count', 5000)
            )
            api = self._get_other_api(self._get_chat_api_index(chat.who))
            updated = system.update_memory(
                str(getattr(chat, "who", "") or "").strip(),
                messages,
                api,
                chat_type='private',
                protected_count=getattr(
                    self.config,
                    'chat_memory_protected_recent_count',
                    CHAT_MEMORY_DEFAULT_PROTECTED_RECENT_COUNT,
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
        chat_type = getattr(chat, 'chat_type', 'private')
        if chat_type == 'group' or chat_name in getattr(self.config, 'group', []):
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
            chat_names = list_chat_names()
        except Exception as exc:
            log(level="WARNING", message=f"会话记忆启动补偿扫描失败：{exc}")
            return 0
        count = 0
        for chat_name in chat_names:
            chat_name = str(chat_name or "").strip()
            if not chat_name:
                continue
            if chat_name in getattr(self.config, 'group', []):
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
        3. 管理员命令解析
        4. 普通好友 AI 回复

        :param chat:    聊天窗口子对象
        :param message: 消息对象
        :return:        发送结果
        """
        result = True  # 默认返回成功（WxResponse 类型）

        route = message_routing.route_process_message(self, chat, message)
        action = route.get("action", "skip")
        recovery_chat_type = str(getattr(message, "_wxbot_recovery_chat_type", "") or "").strip().lower()
        if recovery_chat_type == "group" and action != "group_ai":
            self._resolve_group_unanswered("", message)
            return True
        if action == "skip":
            return True
        if action == "takeover_mirror":
            return takeover_runtime.mirror_takeover_message_to_admin(self, chat, message)
        if action == "group_keyword_reply":
            log(level="DEBUG", message=f"群组 {chat.who}：命中关键词回复，内容：{message.content}")
            reply_actions = route.get("reply_actions", [])
            send_success, result = self._send_keyword_reply_actions(
                chat,
                reply_actions,
                delivery_id=self._stable_inbound_delivery_id("group-keyword", chat, message),
            )
            if send_success:
                self._record_reply_metric_success(chat.who, chat_type="group")
                self._record_keyword_reply_success(chat.who, chat_type="group", action_count=len(reply_actions))
            time.sleep(1)
            return result
        if action == "group_ai":
            group_record_id = ""
            unanswered_store = getattr(self, "_unanswered_inbound_store", None)
            if unanswered_store is not None:
                recovery_record_id = str(getattr(message, "_wxbot_recovery_record_id", "") or "")
                if recovery_record_id:
                    group_record_id = recovery_record_id
                    unanswered_store.set_status(group_record_id, "replaying")
                else:
                    group_record_id = unanswered_store.begin(chat.who, message, chat_type="group")
                unanswered_store.set_status(group_record_id, "ai_started")
            group_image_reply_context_used = False
            content_without_at = re.sub(self.config.AtMe, "", message.content).strip()
            log(level="DEBUG", message=f"群组 {chat.who}：触发 AI 回复，内容：{content_without_at}")
            content_with_sender = f"{message.sender}: {format_model_message_text({'type': getattr(message, 'type', ''), 'content': content_without_at})}"
            model_group_user_message = build_current_turn_user_message(content_with_sender)
            group_voice_candidate_hit = False
            group_preprocess_fallback_should_mark = False
            try:
                history = []
                if self.config.memory_switch and self.config.memory_context_switch and self.memory_manager:
                    self._repair_group_context_before_ai(chat, message)
                    history = self._get_model_context_history(chat.who)
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
                    pending_visual_context = self._get_pending_visual_context(chat.who)
                    if pending_visual_context:
                        reply = self._reply_group_image_message(
                            chat,
                            message,
                            history,
                            pending_visual_context.get("image_paths", []),
                            content_without_at,
                        )
                        group_image_reply_context_used = True
                    else:
                        group_api = self._get_group_api(chat.who)
                        reply = group_api.chat(model_group_user_message, prompt=_effective_group_prompt, history=history)
                else:
                    group_api = self._get_group_api(chat.who)
                    reply = group_api.chat(model_group_user_message, prompt=_effective_group_prompt, history=history)
            except Exception as e:
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
                        self._resolve_group_unanswered(group_record_id, message)
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
                    self._resolve_group_unanswered(group_record_id, message)
                    return True
                if preprocess_result["status"] == "silent":
                    self._resolve_group_unanswered(group_record_id, message)
                    return True
                if preprocess_result["status"] == "api_error":
                    group_api_error_reply = True
                    if getattr(self.config, "api_error_reply_once", False) and group_user_key:
                        user_data = self._reply_once_user_data(group_user_key)
                        if user_data.get("api_err_notified"):
                            self._resolve_group_unanswered(group_record_id, message)
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
                    if self._try_send_voice_reply(
                        chat,
                        clean_reply,
                        state_key=f"group:{chat.who}",
                        cooldown_minutes=getattr(self.config, 'group_voice_reply_cooldown_minutes', 0),
                        limit_count=getattr(self.config, 'group_voice_reply_limit_count', 99),
                        limit_hours=getattr(self.config, 'group_voice_reply_limit_hours', 24),
                        context_text=group_context_text,
                        unanswered_record_id=group_record_id,
                    ):
                        self._save_voice_reply_state()
                        self._record_reply_metric_success(chat.who, chat_type="group")
                        log(level="SUCCESS", message=f"群聊 {chat.who}：本轮回复已发送")
                        self._resolve_group_unanswered(group_record_id, message)
                        return True

            _at_msg = self.config.group_reply_at_msg
            _quote = self.config.group_reply_quote
            sent_any = False
            last_index = max(0, len(parts) - 1)
            for i, part in enumerate(parts):
                if self.is_stop_requested():
                    log(message=f"群聊 {chat.who}：机器人正在停止，已停止发送回复")
                    break
                self._human_delay_for_reply_part(
                    part_text=part,
                    split_continuation=(i > 0),
                    is_last=(i == last_index),
                    delay_enabled=bool(
                        getattr(getattr(self, "config", None), "group_split_reply_switch", False)
                        and getattr(getattr(self, "config", None), "group_split_reply_delay_switch", True)
                    ),
                )
                if self.is_stop_requested():
                    log(message=f"群聊 {chat.who}：机器人正在停止，已停止发送剩余回复")
                    break
                self._mark_unanswered_send_started(group_record_id)
                if _quote and i == 0:
                    if isinstance(chat, OwnedChat):
                        result = self._ui_quote_message(
                            chat,
                            message,
                            part,
                            at=message.sender if _at_msg else "",
                        )
                    else:
                        with wechat_ui_actions.hold(self):
                            with self._get_chat_send_lock(chat.who):
                                result = message.quote(part, at=message.sender if _at_msg else None)
                else:
                    if isinstance(chat, OwnedChat):
                        result = chat.SendMsg(
                            msg=part,
                            at=message.sender if _at_msg and i == 0 else None,
                        )
                    else:
                        with wechat_ui_actions.hold(self):
                            with self._get_chat_send_lock(chat.who):
                                result = chat.SendMsg(
                                    msg=part,
                                    at=message.sender if _at_msg and i == 0 else None,
                                )
                sent_any = sent_any or ReplyCountStore.was_send_success(result)

            if sent_any:
                if group_image_reply_context_used and not group_api_error_reply:
                    if self._pending_visual_context_ready_to_clear(chat.who):
                        self._clear_pending_visual_context(chat.who)
                    else:
                        log(message=f"群聊 {chat.who}：图片摘要尚未回写，暂保留最近图片上下文")
                if group_api_error_should_mark:
                    group_user_key = self._get_group_reply_once_key(chat, message)
                    if group_user_key:
                        self.reply_count_store.mark_api_err_notified(group_user_key)
                if group_preprocess_fallback_should_mark:
                    group_user_key = self._get_group_reply_once_key(chat, message)
                    if group_user_key:
                        self.reply_count_store.mark_preprocess_fallback_notified(group_user_key)
                self._record_reply_metric_success(chat.who, chat_type="group")
                log(level="SUCCESS", message=f"群聊 {chat.who}：本轮回复已发送")
            self._resolve_group_unanswered(group_record_id, message)
            return result

        if action == "admin_command":
            result = self.process_command(chat, message)
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

        try:
            moments_api_index = int(config_data.get('moments_api_index', getattr(self.config, 'moments_api_index', 0)))
        except (TypeError, ValueError):
            moments_api_index = 0
        if api_configs:
            moments_api_index = max(0, min(len(api_configs) - 1, moments_api_index))
        else:
            moments_api_index = 0
        self.config.moments_api_index = moments_api_index
        self.config.config['moments_api_index'] = moments_api_index

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

    def _build_image_user_message(self, chat_type="private", sender="", attached_text="", image_count=1, visual_notes=None):
        return build_image_user_message(
            chat_type,
            sender=sender,
            attached_text=attached_text,
            image_count=image_count,
            visual_notes=visual_notes,
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
            visual_notes=visual_notes,
            on_visual_notes=lambda paths, notes: self._remember_visual_notes(chat_name, paths, notes),
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
                self._remember_visual_notes(chat.who, normalized_paths, normalized_notes)
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

    def _generate_visual_notes_for_image_paths(self, chat_type, image_paths, *, sender="", attached_text=""):
        normalized_paths = [
            str(path or "").strip()
            for path in (image_paths or [])
            if str(path or "").strip()
        ]
        if not normalized_paths:
            return []
        notes = []
        recognition_api = self._get_image_recognition_api_for_chat(chat_type)
        for image_path in normalized_paths:
            note = self._get_vision_bridge().analyze(
                image_path=image_path,
                recognition_api=recognition_api,
                chat_type=chat_type,
                sender=sender,
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

    def _save_incoming_image_memory_message(self, chat, message):
        if not getattr(getattr(self, "config", None), "memory_switch", False):
            return False
        if self._private_outbound_echo_reservation_owned_by_fallback(message):
            setattr(message, "_wxbot_memory_persisted", True)
            return True
        memory_manager = getattr(self, "memory_manager", None)
        save_message = getattr(memory_manager, "save_message", None)
        if not callable(save_message):
            return False
        image_paths = self._extract_message_image_paths(message)
        if not image_paths:
            return False
        chat_type = getattr(chat, "chat_type", "private")
        try:
            memory_chat_name = str(getattr(chat, "who", "") or "").strip()
            save_message(
                chat_name=memory_chat_name,
                sender=getattr(message, "sender", ""),
                content="[图片]",
                msg_type="image",
                msg_attr=getattr(message, "attr", "friend"),
                max_count=getattr(self.config, "memory_max_count", 1000),
                message_time=getattr(message, "time", None) or getattr(message, "_wxbot_received_at", None),
                image_paths=image_paths,
            )
            setattr(message, "_wxbot_memory_persisted", True)
        except Exception as exc:
            log(level="WARNING", message=f"写入图片记忆失败：{exc}")
            return False
        try:
            self._mark_chat_memory_dirty(
                SimpleNamespace(who=memory_chat_name, chat_type=chat_type),
                message,
            )
        except Exception as exc:
            log(level="WARNING", message=f"标记图片记忆待维护失败：{exc}")
        return True

    def _reply_group_image_message(self, chat, message, history, image_paths=None, attached_text=""):
        normalized_paths = (
            [str(path or "").strip() for path in image_paths if str(path or "").strip()]
            if isinstance(image_paths, (list, tuple))
            else [str(image_paths or "").strip()] if str(image_paths or "").strip() else []
        )
        normalized_notes = self._normalize_visual_note_slots(normalized_paths, None)
        if normalized_paths:
            try:
                normalized_notes = self._generate_visual_notes_for_image_paths(
                    "group",
                    normalized_paths,
                    sender=getattr(message, "sender", ""),
                    attached_text="",
                )
                self._remember_visual_notes(chat.who, normalized_paths, normalized_notes)
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
            visual_notes=normalized_notes,
        )

    def _get_chat_send_lock(self, chat_name):
        return nullcontext()

    def _ensure_message_runtime_state(self):
        if not hasattr(self, '_incoming_seen_lock'):
            self._incoming_seen_lock = threading.Lock()
        if not hasattr(self, '_incoming_seen_ids'):
            self._incoming_seen_ids = {}
        if not hasattr(self, '_incoming_seen_fingerprints'):
            self._incoming_seen_fingerprints = {}
        if not hasattr(self, '_chat_merge_lock'):
            self._chat_merge_lock = threading.Lock()
        if not hasattr(self, '_private_message_pipelines'):
            self._private_message_pipelines = {}
        if not hasattr(self, '_pending_private_voice_transcription'):
            self._pending_private_voice_transcription = {}
        if not hasattr(self, '_pending_private_voice_sequence'):
            self._pending_private_voice_sequence = {}
        if not hasattr(self, '_private_message_sequence_by_chat'):
            self._private_message_sequence_by_chat = {}
        if not hasattr(self, '_private_reply_runtime_turns'):
            self._private_reply_runtime_turns = {}
        if not hasattr(self, '_private_reply_persisted_echoes'):
            self._private_reply_persisted_echoes = {}
        if not hasattr(self, '_private_outbound_echoes'):
            self._private_outbound_echoes = {}
        if not hasattr(self, '_private_outbound_echo_fallback_stopped'):
            self._private_outbound_echo_fallback_stopped = False
        if not hasattr(self, '_private_outbound_history_write_lock'):
            self._private_outbound_history_write_lock = threading.RLock()
        if not hasattr(self, '_pending_visual_contexts'):
            self._pending_visual_contexts = {}
        if not hasattr(self, '_memory_context_repair_startup_done'):
            self._memory_context_repair_startup_done = set()
        if not hasattr(self, '_memory_context_repair_restore_pending'):
            self._memory_context_repair_restore_pending = set()
        if not hasattr(self, '_memory_context_repair_last_at'):
            self._memory_context_repair_last_at = {}
        if not hasattr(self, '_memory_context_repair_lock'):
            self._memory_context_repair_lock = threading.Lock()

    def _next_private_message_sequence(self, chat_name):
        self._ensure_message_runtime_state()
        name = str(chat_name or "").strip()
        if not name:
            return 0
        with self._chat_merge_lock:
            sequence = self._private_message_sequence_by_chat.get(name, 0) + 1
            self._private_message_sequence_by_chat[name] = sequence
            return sequence

    def _get_private_message_sequence(self, chat_name):
        self._ensure_message_runtime_state()
        name = str(chat_name or "").strip()
        if not name:
            return 0
        with self._chat_merge_lock:
            return self._private_message_sequence_by_chat.get(name, 0)

    def _mark_message_seen(self, chat_name, message):
        self._ensure_message_runtime_state()
        msg_id = getattr(message, "id", None)
        msg_hash_text = getattr(message, "hash_text", None)
        if msg_id not in {None, ""}:
            key = f"id:{chat_name}:{msg_id}"
        elif msg_hash_text not in {None, ""}:
            key = f"hash_text:{chat_name}:{msg_hash_text}"
        else:
            return True
        now = time.time()
        with self._incoming_seen_lock:
            if len(self._incoming_seen_ids) > 2000:
                cutoff = now - 3600
                self._incoming_seen_ids = {
                    k: v for k, v in self._incoming_seen_ids.items() if v >= cutoff
                }
            if key in self._incoming_seen_ids:
                return False
            self._incoming_seen_ids[key] = now
            return True

    def _mark_message_content_fingerprint_seen(self, chat_name, message, ttl=2):
        # Without a runtime-stable row identity, two legitimate consecutive
        # messages cannot be distinguished from a duplicate callback.
        return True

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
        limit_hours = getattr(self.config, "text_reply_limit_hours", 24)
        return self.reply_count_store.get_user(user_key, now=datetime.now(), limit_hours=limit_hours)

    def _ensure_pending_private_voice_transcription_state(self):
        if not hasattr(self, "_pending_private_voice_transcription") or self._pending_private_voice_transcription is None:
            self._pending_private_voice_transcription = {}
        if not hasattr(self, "_pending_private_voice_sequence") or self._pending_private_voice_sequence is None:
            self._pending_private_voice_sequence = {}

    @staticmethod
    def _pending_voice_item_expired(item):
        attempts = int(item.get("reread_attempts") or 0)
        return attempts >= VOICE_TRANSCRIPTION_MAX_REREAD_ATTEMPTS

    @staticmethod
    def _pending_voice_item_deadline_reached(item):
        first_seen_at = float(item.get("first_seen_at") or time.time())
        max_wait = VOICE_TRANSCRIPTION_RETRY_DELAY_SECONDS * VOICE_TRANSCRIPTION_MAX_REREAD_ATTEMPTS
        return time.time() - first_seen_at >= max_wait

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
        self._private_message_sequence_by_chat[name] = self._private_message_sequence_by_chat.get(name, 0) + 1
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
        self._ensure_pending_private_voice_transcription_state()
        with self._chat_merge_lock:
            task = self._pending_private_voice_transcription.get(name)
            if not isinstance(task, dict):
                task = {"chat": chat, "items": {}, "timer": None}
                self._pending_private_voice_transcription[name] = task
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
        }
        created_task = False
        with self._chat_merge_lock:
            task = self._pending_private_voice_transcription.get(name)
            if not isinstance(task, dict):
                task = {"chat": chat_ref, "items": {}, "source_keys": set(), "timer": None}
                self._pending_private_voice_transcription[name] = task
            task.setdefault("source_keys", set())
            already_pending = bool(source_key and source_key in task["source_keys"])
            if already_pending:
                task["chat"] = chat_ref
                return True
            sequence = self._pending_private_voice_sequence.get(name, 0) + 1
            self._pending_private_voice_sequence[name] = sequence
            key = f"voice:{name}:{sequence}"
            item["key"] = key
            task["chat"] = chat_ref
            if source_key:
                task["source_keys"].add(source_key)
            task["items"][key] = item
            self._insert_pending_private_voice_placeholder_locked(name, chat, message, key)
            if not task.get("timer"):
                task["timer"] = self._schedule_private_message_timer(
                    VOICE_TRANSCRIPTION_RETRY_DELAY_SECONDS,
                    self._flush_pending_private_voice_transcription,
                    chat_ref,
                )
                created_task = True
        return True

    def _read_pending_voice_snapshot(self, chat_name):
        name = str(chat_name or "").strip()
        if not name:
            return []
        get_subwindow = getattr(getattr(self, "wx", None), "GetSubWindow", None)
        if not callable(get_subwindow):
            return []
        current_chat = get_subwindow(nickname=name)
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
        self._ensure_pending_private_voice_transcription_state()
        with self._chat_merge_lock:
            task = self._pending_private_voice_transcription.pop(name, None)
            if isinstance(task, dict):
                task["timer"] = None
        if not isinstance(task, dict):
            return True
        items = list((task.get("items") or {}).values())
        if not items or self.is_stop_requested():
            return True
        release_wechat_lock = wechat_ui_actions.try_acquire(self)
        if not release_wechat_lock:
            expired = [
                item for item in items
                if self._pending_voice_item_expired(item) or self._pending_voice_item_deadline_reached(item)
            ]
            pending = [item for item in items if item not in expired]
            if pending:
                self._reschedule_pending_private_voice_transcription(chat, pending, reason="重读时微信操作锁忙")
            if expired:
                with self._chat_merge_lock:
                    for item in expired:
                        item_key = item.get("key")
                        if item_key:
                            self._drop_pending_private_voice_placeholder_locked(name, item_key)
                    self._wake_private_batch_if_pending_voice_unblocked_locked(name, chat)
                log(message=f"私聊 {name}：{len(expired)} 条语音重读 {VOICE_TRANSCRIPTION_MAX_REREAD_ATTEMPTS} 次仍未得到有效文字，已静默忽略")
            return True
        try:
            for item in items:
                item["reread_attempts"] = int(item.get("reread_attempts") or 0) + 1
            snapshot = self._read_pending_voice_snapshot(name)
            matched = message_routing.match_pending_voice_snapshot(items, snapshot)
            for item in items:
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
                with self._chat_merge_lock:
                    for item in expired:
                        item_key = item.get("key")
                        if item_key:
                            self._drop_pending_private_voice_placeholder_locked(name, item_key)
                    self._wake_private_batch_if_pending_voice_unblocked_locked(name, chat)
                log(message=f"私聊 {name}：{len(expired)} 条语音重读 {VOICE_TRANSCRIPTION_MAX_REREAD_ATTEMPTS} 次仍未得到有效文字，已静默忽略，最后一次重读失败：{exc}")
            return True
        finally:
            release_wechat_lock()
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
                            self._drop_pending_private_voice_placeholder_locked(name, item_key)
                            self._wake_private_batch_if_pending_voice_unblocked_locked(name, chat)
                else:
                    pending_items.append(item)
                continue
            state = message_routing.voice_content_state(getattr(resolved, "content", ""))
            if state == "valid":
                if str((item.get("signature") or {}).get("attr") or "") != "friend":
                    setattr(resolved, "_wxbot_media_prepared", True)
                    self.message_handle_callback(resolved, chat)
                    continue
                replaced = False
                if item_key:
                    with self._chat_merge_lock:
                        replaced = self._replace_pending_private_voice_placeholder_locked(name, item_key, resolved)
                        self._wake_private_batch_if_pending_voice_unblocked_locked(name, chat)
                if not replaced:
                    log(message=f"私聊 {name}：语音识别结果已过期，已静默忽略")
                    continue
                self._save_private_incoming_memory_message(chat, resolved)
            elif state == "failed":
                if item_key:
                    with self._chat_merge_lock:
                        self._drop_pending_private_voice_placeholder_locked(name, item_key)
                        self._wake_private_batch_if_pending_voice_unblocked_locked(name, chat)
                log(message=f"私聊 {name}：语音识别失败，未得到有效文字，已静默忽略")
            elif self._pending_voice_item_expired(item) or self._pending_voice_item_deadline_reached(item):
                expired_items.append(item)
                if item_key:
                    with self._chat_merge_lock:
                        self._drop_pending_private_voice_placeholder_locked(name, item_key)
                        self._wake_private_batch_if_pending_voice_unblocked_locked(name, chat)
            else:
                pending_items.append(item)
        if pending_items:
            self._reschedule_pending_private_voice_transcription(chat, pending_items, reason="结果仍未就绪")
        if expired_items:
            log(message=f"私聊 {name}：{len(expired_items)} 条语音重读 {VOICE_TRANSCRIPTION_MAX_REREAD_ATTEMPTS} 次仍未得到有效文字，已静默忽略")
        return True

    def _save_incoming_memory_message(self, chat, message, *, chat_type=None):
        self._ensure_message_runtime_state()
        reservation_id = str(getattr(message, "_wxbot_outbound_echo_reservation", "") or "").strip()
        guard = self._private_outbound_history_write_lock if reservation_id else nullcontext()
        with guard:
            if reservation_id and self._private_outbound_echo_fallback_stopped:
                return False
            return self._save_incoming_memory_message_locked(chat, message, chat_type=chat_type)

    def _save_incoming_memory_message_locked(self, chat, message, *, chat_type=None):
        if bool(getattr(message, "_wxbot_memory_persisted", False)):
            return True
        if not getattr(getattr(self, "config", None), "memory_switch", False):
            return False
        memory_manager = getattr(self, "memory_manager", None)
        save_message = getattr(memory_manager, "save_message", None)
        if not callable(save_message) or self._should_skip_message_memory(chat, message):
            return False
        callback_claim = self._claim_private_outbound_echo_callback_write(message)
        if callback_claim == "owned":
            setattr(message, "_wxbot_memory_persisted", True)
            return True
        try:
            memory_chat_name = str(getattr(chat, "who", "") or "").strip()
            if getattr(message, "type", "") == "image":
                saved = self._save_incoming_image_memory_message(chat, message)
                if saved:
                    setattr(message, "_wxbot_memory_persisted", True)
                    self._commit_private_outbound_echo_reservation(message)
                else:
                    self._release_private_outbound_echo_callback_write(message)
                return saved
            save_message(
                chat_name=memory_chat_name,
                sender=getattr(message, "sender", ""),
                content=(
                    strip_voice_duration_metadata(getattr(message, "content", ""))
                    if getattr(message, "type", "") == "voice"
                    else getattr(message, "content", "")
                ),
                msg_type=getattr(message, "type", "text"),
                msg_attr=getattr(message, "attr", "friend"),
                max_count=getattr(self.config, "memory_max_count", 1000),
                message_time=getattr(message, "time", None) or getattr(message, "_wxbot_received_at", None),
            )
            setattr(message, "_wxbot_memory_persisted", True)
            self._commit_private_outbound_echo_reservation(message)
        except Exception as exc:
            self._release_private_outbound_echo_callback_write(message)
            log(level="WARNING", message=f"写入消息记忆失败: {exc}")
            return False
        try:
            self._mark_chat_memory_dirty(
                SimpleNamespace(
                    who=memory_chat_name,
                    chat_type=chat_type or getattr(chat, "chat_type", "private"),
                ),
                message,
            )
        except Exception as exc:
            log(level="WARNING", message=f"标记消息记忆待维护失败: {exc}")
        return True

    def _save_private_incoming_memory_message(self, chat, message):
        return self._save_incoming_memory_message(chat, message, chat_type="private")

    def _memory_context_repair_config(self):
        return {
            "cooldown": DEFAULT_CONTEXT_REPAIR_COOLDOWN_SECONDS,
            "anchor_count": DEFAULT_ANCHOR_RECENT_COUNT,
            "visible_limit": DEFAULT_VISIBLE_LIMIT,
            "local_history_limit": DEFAULT_LOCAL_HISTORY_LIMIT,
        }

    def _mark_context_repair_needed_after_restore(self, chat_name):
        chat_name = str(chat_name or "").strip()
        if not chat_name:
            return False
        self._ensure_message_runtime_state()
        with self._memory_context_repair_lock:
            self._memory_context_repair_restore_pending.add(chat_name)
        return True

    def _context_repair_reasons(self, chat, message, memory_chat_name):
        chat_name = str(getattr(chat, "who", "") or "").strip()
        if not chat_name:
            return []
        self._ensure_message_runtime_state()
        reasons = []
        with self._memory_context_repair_lock:
            if chat_name not in self._memory_context_repair_startup_done:
                reasons.append("startup_first_reply")
            if chat_name in self._memory_context_repair_restore_pending:
                reasons.append("restore_first_reply")

        try:
            tail = self.memory_manager.get_messages(memory_chat_name, 8) if self.memory_manager else []
            if not current_message_found_near_tail(tail, message, tail_count=8):
                reasons.append("current_message_missing_from_memory_tail")
        except Exception:
            reasons.append("memory_tail_check_failed")
        return reasons

    def _consume_context_repair_reasons(self, chat_name, reasons):
        chat_name = str(chat_name or "").strip()
        if not chat_name or not reasons:
            return
        self._ensure_message_runtime_state()
        with self._memory_context_repair_lock:
            if "startup_first_reply" in reasons:
                self._memory_context_repair_startup_done.add(chat_name)
            if "restore_first_reply" in reasons:
                self._memory_context_repair_restore_pending.discard(chat_name)

    def _context_repair_success_ttl_allows(self, chat_key, ttl_seconds):
        chat_key = str(chat_key or "").strip()
        if not chat_key:
            return False
        self._ensure_message_runtime_state()
        now = time.time()
        with self._memory_context_repair_lock:
            last = float(self._memory_context_repair_last_at.get(chat_key, 0) or 0)
        return not (ttl_seconds > 0 and now - last < ttl_seconds)

    def _mark_context_repair_success(self, chat_key):
        chat_key = str(chat_key or "").strip()
        if not chat_key:
            return
        self._ensure_message_runtime_state()
        with self._memory_context_repair_lock:
            self._memory_context_repair_last_at[chat_key] = time.time()

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
        return messages[-limit:] if len(messages) > limit else messages

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

    def _append_context_repair_messages(self, memory_chat_name, entries, *, chat_type="private"):
        if not entries or not self.memory_manager:
            return {"added": 0, "total": 0}
        result = self.memory_manager.append_missing_messages(
            memory_chat_name,
            entries,
            getattr(self.config, "memory_max_count", 5000),
            reconcile_visible_snapshot=True,
            chat_type=chat_type,
            anchor_recent_count=DEFAULT_ANCHOR_RECENT_COUNT,
        )
        if int(result.get("added", 0) or 0) > 0:
            self._mark_chat_memory_dirty(
                SimpleNamespace(who=memory_chat_name, chat_type=chat_type),
                SimpleNamespace(type="text", attr="friend", content="[上下文补洞]"),
            )
        return result

    def _mark_unanswered_send_started(self, record_id=""):
        store = getattr(self, "_unanswered_inbound_store", None)
        if store is None:
            return
        if not record_id:
            context = getattr(self, "_unanswered_inbound_context", None)
            record_id = str(getattr(context, "record_id", "") or "") if context is not None else ""
        if record_id:
            store.set_status(record_id, "send_started")

    def _mark_unanswered_uncertain(self, record_id=""):
        store = getattr(self, "_unanswered_inbound_store", None)
        if store is None:
            return
        if not record_id:
            context = getattr(self, "_unanswered_inbound_context", None)
            record_id = str(getattr(context, "record_id", "") or "") if context is not None else ""
        if record_id:
            store.set_status(record_id, "uncertain")

    def _repair_group_context_before_ai(self, chat, message):
        chat_name = str(getattr(chat, "who", "") or "").strip()
        if not (
            getattr(chat, "chat_type", "private") == "group"
            or chat_name in getattr(self.config, "group", [])
        ):
            return False
        if not (
            getattr(getattr(self, "config", None), "memory_switch", False)
            and getattr(getattr(self, "config", None), "memory_context_switch", False)
            and self.memory_manager
        ):
            return False
        cfg = self._memory_context_repair_config()
        if not chat_name:
            return False
        cooldown_key = f"group:{chat_name}"
        if not self._context_repair_success_ttl_allows(cooldown_key, cfg["cooldown"]):
            return False

        try:
            self._save_incoming_memory_message(chat, message, chat_type="group")
            local_history = self.memory_manager.get_messages(chat_name, cfg["local_history_limit"]) or []
            release_wechat_lock = wechat_ui_actions.try_acquire(self)
            if not release_wechat_lock:
                return False
            try:
                visible_messages = self._read_visible_context_messages(chat, cfg["visible_limit"])
            finally:
                release_wechat_lock()
            visible_messages = snapshot_messages_through_current(visible_messages, message)
            visible_entries = normalize_wechat_snapshot(
                visible_messages,
                source="wechat_context_repair_group",
                fallback_tail_time=(
                    getattr(message, "time", "")
                    or getattr(message, "_wxbot_received_at", "")
                ),
            )
            result = self._append_context_repair_messages(chat_name, visible_entries, chat_type="group")
            added = int(result.get("added", 0) or 0)
            anchor_found = bool(result.get("anchor_found"))
            self._log_context_repair_result(
                "group",
                chat_name,
                "ui",
                added,
                suffix="未找到锚点，已按当前可见快照补入缺失消息" if not anchor_found else "",
            )
            self._mark_context_repair_success(cooldown_key)
            return bool(added or anchor_found)
        except Exception as exc:
            log(level="WARNING", message=f"群聊 {chat_name}：上下文补洞失败，已继续原回复流程，详情：{exc}")
            return False

    def _repair_private_context_before_ai(self, chat, message):
        chat_name = str(getattr(chat, "who", "") or "").strip()
        if getattr(chat, "chat_type", "private") == "group" or chat_name in getattr(self.config, "group", []):
            return False
        if not (
            getattr(getattr(self, "config", None), "memory_switch", False)
            and getattr(getattr(self, "config", None), "memory_context_switch", False)
            and self.memory_manager
        ):
            return False
        cfg = self._memory_context_repair_config()
        if not chat_name:
            return False
        memory_chat_name = str(chat_name or "").strip()
        repair_key = f"private:{chat_name}"
        reasons = self._context_repair_reasons(chat, message, memory_chat_name)
        if not reasons:
            return False
        if not self._context_repair_success_ttl_allows(repair_key, cfg["cooldown"]):
            return False

        try:
            local_history = self.memory_manager.get_messages(memory_chat_name, cfg["local_history_limit"]) or []
            release_wechat_lock = wechat_ui_actions.try_acquire(self)
            if not release_wechat_lock:
                return False
            try:
                visible_messages = self._read_visible_context_messages(chat, cfg["visible_limit"])
            finally:
                release_wechat_lock()
        except Exception as exc:
            log(level="WARNING", message=f"私聊 {chat_name}：上下文补洞失败，已继续原回复流程，详情：{exc}")
            return False

        try:
            visible_messages = snapshot_messages_through_current(visible_messages, message)
            visible_entries = normalize_wechat_snapshot(
                visible_messages,
                source="wechat_context_repair",
                fallback_tail_time=(
                    getattr(message, "time", "")
                    or getattr(message, "_wxbot_received_at", "")
                ),
            )
            result = self._append_context_repair_messages(memory_chat_name, visible_entries)
            added = int(result.get("added", 0) or 0)
            anchor_found = bool(result.get("anchor_found"))
            self._log_context_repair_result(
                "private",
                chat_name,
                "ui",
                added,
                suffix="未找到锚点，已按当前可见快照补入缺失消息" if not anchor_found else "",
            )
            self._consume_context_repair_reasons(chat_name, reasons)
            self._mark_context_repair_success(repair_key)
            return bool(added or anchor_found)
        except Exception as exc:
            log(level="WARNING", message=f"私聊 {chat_name}：上下文补洞失败，已继续原回复流程，详情：{exc}")
            return False

    def _build_merged_private_message(self, messages):
        return build_merged_private_message(
            messages,
            on_extra_image=lambda _image_path: log(
                level="INFO",
                message=f"私聊连续消息收到超过 {MAX_MERGED_PRIVATE_IMAGES} 张图片，超出部分已忽略",
            ),
        )

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
            return
        with self._chat_merge_lock:
            pipeline = self._private_message_pipelines.pop(name, None)
        if not isinstance(pipeline, dict):
            return
        self._cancel_timer(pipeline.get("idle_timer"))
        self._cancel_timer(pipeline.get("max_timer"))

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
                    log(level="ERROR", message=f"私聊连续消息处理失败：{exc}\n{traceback.format_exc()}")
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
        is_recovery = bool(getattr(message, "_wxbot_unanswered_recovery", False))
        if is_recovery:
            with self._chat_merge_lock:
                pipeline = self._private_message_pipeline(chat.who)
                if not pipeline:
                    return True
                self._enqueue_private_message_batch_locked(pipeline, [message])
                self._start_private_message_worker_locked(chat, pipeline)
            return True
        if getattr(message, '_voice_transcription_failed', False):
            if not getattr(message, "_wxbot_seen_at_ingress", False) and not self._mark_message_seen(chat.who, message):
                log(message=f"私聊 {chat.who}：重复失败语音已忽略")
                return True
            if (
                not getattr(message, "_wxbot_seen_at_ingress", False)
                and not self._mark_message_content_fingerprint_seen(chat.who, message)
            ):
                log(message=f"私聊 {chat.who}：短时间重复失败语音回调已忽略")
                return True
            log(message=f"私聊 {chat.who}：语音识别失败，未得到有效文字，已静默忽略")
            return True
        if self._should_skip_private_ai_message(message):
            return True
        if not getattr(message, "_wxbot_seen_at_ingress", False) and not self._mark_message_seen(chat.who, message):
            log(message=f"私聊 {chat.who}：重复消息已忽略")
            return True
        if (
            not getattr(message, "_wxbot_seen_at_ingress", False)
            and not self._mark_message_content_fingerprint_seen(chat.who, message)
        ):
            log(message=f"私聊 {chat.who}：短时间重复回调已忽略")
            return True
        self._save_private_incoming_memory_message(chat, message)
        if not getattr(message, "_wxbot_sequence_advanced", False):
            self._next_private_message_sequence(chat.who)
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
            self._set_pending_visual_context(chat.who, pending_image_paths)
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

    def _should_track_private_outbound_echo(self, chat_name):
        name = str(chat_name or "").strip()
        if not name:
            return False
        config = getattr(self, "config", None)
        if name == getattr(config, "cmd", ""):
            return False
        if name in getattr(config, "group", []):
            return False
        return True

    @staticmethod
    def _private_outbound_echo_type_for_file(path):
        ext = os.path.splitext(str(path or "").strip())[1].lower()
        if ext in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif"}:
            return "image"
        if ext in {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v"}:
            return "video"
        return "file"

    def _normalize_private_outbound_echo_type(self, msg_type="", *, path=""):
        kind = str(msg_type or "").strip().lower()
        if kind == "audio":
            kind = "voice"
        if kind == "file" and str(path or "").strip():
            kind = self._private_outbound_echo_type_for_file(path)
        if kind in {
            "text",
            "voice",
            "image",
            "file",
            "video",
            "link",
            "miniapp",
            "merge",
            "quote",
            "emotion",
            "note",
            "location",
            "personal_card",
        }:
            return kind
        return "unknown"

    @staticmethod
    def _private_outbound_echo_ttl(kind):
        if kind == "text":
            return PRIVATE_REPLY_ECHO_DEDUPE_SECONDS
        if kind == "unknown":
            return PRIVATE_OUTBOUND_ECHO_UNKNOWN_SECONDS
        return PRIVATE_OUTBOUND_ECHO_NON_TEXT_SECONDS

    @staticmethod
    def _private_outbound_echo_types_match(expected, actual):
        if expected == actual:
            return True
        if expected == "unknown" and actual != "text":
            return True
        return False

    def _remember_private_outbound_echo(
        self,
        chat_name,
        msg_type="text",
        content="",
        *,
        source="",
        path="",
        count=1,
        memory_persisted=False,
        fallback_content="",
        echo_group_id="",
        primary_memory_writer=False,
    ):
        name = str(chat_name or "").strip()
        if not self._should_track_private_outbound_echo(name):
            return False
        kind = self._normalize_private_outbound_echo_type(msg_type, path=path)
        text = str(content or "").strip()
        if kind == "text" and not text:
            return False
        try:
            count = max(1, int(count or 1))
        except Exception:
            count = 1
        self._ensure_message_runtime_state()
        now = time.time()
        self._flush_expired_private_outbound_echo_fallbacks(name, now=now)
        expires_at = now + self._private_outbound_echo_ttl(kind)
        with self._chat_merge_lock:
            echoes = [
                item for item in self._private_outbound_echoes.get(name, [])
                if isinstance(item, dict) and float(item.get("expires_at", 0) or 0) >= now
            ]
            echoes.append({
                "echo_id": uuid.uuid4().hex,
                "echo_group_id": str(echo_group_id or "").strip(),
                "type": kind,
                "content": text,
                "source": str(source or "").strip() or "unknown",
                "remaining": count,
                "expires_at": expires_at,
                "memory_persisted": bool(memory_persisted),
                "fallback_content": str(fallback_content or "").strip(),
                "primary_memory_writer": bool(primary_memory_writer),
            })
            self._private_outbound_echoes[name] = echoes[-PRIVATE_OUTBOUND_ECHO_MAX_PER_CHAT:]
        return True

    def _remember_private_outbound_echo_for_send_result(self, chat_name, result, msg_type="text", content="", *, source="", path="", count=1):
        echo_group_id = self._reserve_private_outbound_echo_for_send(
            chat_name,
            msg_type,
            content,
            source=source,
            path=path,
            count=count,
        )
        return self._settle_private_outbound_echo_send(echo_group_id, result)

    def _reserve_private_outbound_echo_for_send(
        self,
        chat_name,
        msg_type="text",
        content="",
        *,
        source="",
        path="",
        count=1,
        fallback_content="",
        primary_memory_writer=False,
    ):
        echo_group_id = uuid.uuid4().hex
        remembered = self._remember_private_outbound_echo(
            chat_name,
            msg_type,
            content,
            source=source,
            path=path,
            count=count,
            fallback_content=fallback_content,
            echo_group_id=echo_group_id,
            primary_memory_writer=primary_memory_writer,
        )
        return echo_group_id if remembered else ""

    def _reserve_private_outbound_echo_for_action(self, chat_name, action, *, source=""):
        action = dict(action or {})
        action_type = str(action.get("type") or "text").strip().lower()
        if action_type == "file":
            path = str(action.get("path") or "")
            kind = self._normalize_private_outbound_echo_type("file", path=path)
            basename = os.path.basename(path)
            fallback_label = {"image": "[图片]", "video": "[视频]"}.get(kind, "[文件]")
            return self._reserve_private_outbound_echo_for_send(
                chat_name,
                "file",
                source=source,
                path=path,
                fallback_content=f"{fallback_label} {basename}".strip(),
            )
        if action_type in {"voice", "audio"}:
            return self._reserve_private_outbound_echo_for_send(
                chat_name,
                "voice",
                source=source,
                path=str(action.get("path") or ""),
                fallback_content="[语音]",
            )
        text = str(action.get("text") or action.get("content") or "")
        return self._reserve_private_outbound_echo_for_send(
            chat_name,
            "text",
            text,
            source=source,
            fallback_content=text,
        )

    def _settle_private_outbound_echo_send(self, echo_group_id, result):
        echo_group_id = str(echo_group_id or "").strip()
        if not echo_group_id:
            return False
        if result is not None and not ReplyCountStore.was_send_success(result):
            self._mark_private_outbound_echo_group_reported_failed(echo_group_id)
            return False
        self._schedule_private_outbound_echo_fallback("")
        return True

    def _consume_private_outbound_echo(
        self,
        chat_name,
        message=None,
        content=None,
        msg_type=None,
        *,
        return_match=False,
    ):
        name = str(chat_name or "").strip()
        if not name:
            return False
        self._flush_expired_private_outbound_echo_fallbacks(name)
        if message is not None:
            if msg_type is None:
                msg_type = getattr(message, "type", "")
            if content is None:
                content = getattr(message, "content", "")
        kind = self._normalize_private_outbound_echo_type(msg_type)
        text = str(content or "").strip()
        message_id = str(getattr(message, "id", "") or "").strip() if message is not None else ""
        self._ensure_message_runtime_state()
        now = time.time()
        matched = None
        with self._chat_merge_lock:
            echoes = [
                item for item in self._private_outbound_echoes.get(name, [])
                if isinstance(item, dict) and float(item.get("expires_at", 0) or 0) >= now
            ]
            candidates = []
            duplicate_candidates = []
            for index, item in enumerate(echoes):
                expected = str(item.get("type") or "unknown").strip().lower() or "unknown"
                if kind == "text":
                    if expected != "text" or str(item.get("content") or "").strip() != text:
                        continue
                elif not self._private_outbound_echo_types_match(expected, kind):
                    continue
                expected_content = str(item.get("content") or "").strip()
                content_match = bool(
                    expected_content
                    and text
                    and (
                        expected_content == text
                        or expected_content in text
                        or text in expected_content
                    )
                )
                remaining = max(0, int(item.get("remaining", 1) or 0))
                if item.get("reservation_id") or remaining <= 0:
                    matched_message_id = str(item.get("matched_message_id") or "").strip()
                    same_callback = bool(
                        (message_id and matched_message_id and message_id == matched_message_id)
                        or (
                            not message_id
                            and not matched_message_id
                            and (
                                expected_content == text
                                or (not expected_content and not text)
                            )
                        )
                    )
                    if same_callback:
                        duplicate_candidates.append((index, item, content_match))
                    continue
                candidates.append((index, item, content_match))
            selected = None
            if candidates:
                exact_matches = [
                    candidate
                    for candidate in candidates
                    if str(candidate[1].get("content") or "").strip() == text and text
                ]
                substring_matches = [candidate for candidate in candidates if candidate[2]]
                if exact_matches:
                    selected = exact_matches[0]
                elif len(substring_matches) == 1:
                    selected = substring_matches[0]
                elif substring_matches:
                    longest_length = max(
                        len(str(candidate[1].get("content") or "").strip())
                        for candidate in substring_matches
                    )
                    longest_matches = [
                        candidate
                        for candidate in substring_matches
                        if len(str(candidate[1].get("content") or "").strip()) == longest_length
                    ]
                    if len(longest_matches) == 1:
                        selected = longest_matches[0]
                elif not text or all(not str(candidate[1].get("content") or "").strip() for candidate in candidates):
                    selected = candidates[0]
            if selected is not None:
                index, item, _content_match = selected
                matched = dict(item)
                if item.get("primary_memory_writer"):
                    matched["primary_memory_writer"] = True
                if message is not None and return_match and not item.get("memory_persisted"):
                    reservation_id = uuid.uuid4().hex
                    item["reservation_id"] = reservation_id
                    item["matched_message_id"] = message_id
                    item["remaining"] = max(0, int(item.get("remaining", 1) or 1) - 1)
                    matched["reservation_id"] = reservation_id
                elif item.get("memory_persisted"):
                    matched["dedupe_only"] = True
                    item["matched_message_id"] = message_id
                    remaining = max(0, int(item.get("remaining", 1) or 1) - 1)
                    item["remaining"] = remaining
                    if remaining <= 0:
                        item["expires_at"] = min(
                            float(item.get("expires_at", now + 2) or now + 2),
                            now + 2,
                        )
                else:
                    remaining = max(0, int(item.get("remaining", 1) or 1)) - 1
                    if remaining > 0:
                        item["remaining"] = remaining
                    else:
                        del echoes[index]
            elif duplicate_candidates:
                _index, item, _content_match = duplicate_candidates[0]
                matched = dict(item)
                matched["duplicate_reservation"] = True
            if echoes:
                self._private_outbound_echoes[name] = echoes
            else:
                self._private_outbound_echoes.pop(name, None)
        if return_match:
            return matched
        return bool(matched)

    def _commit_private_outbound_echo_reservation(self, message):
        reservation_id = str(getattr(message, "_wxbot_outbound_echo_reservation", "") or "").strip()
        if not reservation_id:
            return False
        now = time.time()
        with self._chat_merge_lock:
            for echoes in self._private_outbound_echoes.values():
                for item in echoes or []:
                    if item.get("reservation_id") != reservation_id:
                        continue
                    item.pop("callback_write_id", None)
                    item.pop("reservation_id", None)
                    item["memory_persisted"] = True
                    item["fallback_content"] = ""
                    if max(0, int(item.get("remaining", 0) or 0)) <= 0:
                        item["expires_at"] = min(
                            float(item.get("expires_at", now + 2) or now + 2),
                            now + 2,
                        )
                    else:
                        item.pop("matched_message_id", None)
                    return True
        return False

    def _mark_private_outbound_echo_group_persisted(self, echo_group_id):
        group_id = str(echo_group_id or "").strip()
        if not group_id:
            return False
        changed = False
        with self._chat_merge_lock:
            for echoes in self._private_outbound_echoes.values():
                for item in echoes or []:
                    if item.get("echo_group_id") != group_id:
                        continue
                    item["memory_persisted"] = True
                    item["fallback_content"] = ""
                    changed = True
        return changed

    def _release_private_outbound_echo_group_to_callback(self, echo_group_id):
        group_id = str(echo_group_id or "").strip()
        if not group_id:
            return False
        changed = False
        with self._chat_merge_lock:
            for echoes in self._private_outbound_echoes.values():
                for item in echoes or []:
                    if item.get("echo_group_id") != group_id:
                        continue
                    item["primary_memory_writer"] = False
                    item["fallback_content"] = ""
                    changed = True
        return changed

    def _private_outbound_echo_reservation_owned_by_fallback(self, message):
        reservation_id = str(getattr(message, "_wxbot_outbound_echo_reservation", "") or "").strip()
        if not reservation_id:
            return False
        with self._chat_merge_lock:
            return any(
                item.get("reservation_id") == reservation_id
                and (item.get("fallback_write_id") or item.get("memory_persisted"))
                for echoes in self._private_outbound_echoes.values()
                for item in echoes or []
                if isinstance(item, dict)
            )

    def _claim_private_outbound_echo_callback_write(self, message):
        reservation_id = str(getattr(message, "_wxbot_outbound_echo_reservation", "") or "").strip()
        if not reservation_id:
            return "none"
        callback_write_id = uuid.uuid4().hex
        with self._chat_merge_lock:
            for echoes in self._private_outbound_echoes.values():
                for item in echoes or []:
                    if item.get("reservation_id") != reservation_id:
                        continue
                    if item.get("fallback_write_id") or item.get("memory_persisted"):
                        return "owned"
                    if item.get("callback_write_id"):
                        return "owned"
                    item["callback_write_id"] = callback_write_id
                    setattr(message, "_wxbot_outbound_echo_callback_write", callback_write_id)
                    return "claimed"
        return "none"

    def _release_private_outbound_echo_callback_write(self, message):
        callback_write_id = str(
            getattr(message, "_wxbot_outbound_echo_callback_write", "") or ""
        ).strip()
        if not callback_write_id:
            return False
        with self._chat_merge_lock:
            for echoes in self._private_outbound_echoes.values():
                for item in echoes or []:
                    if item.get("callback_write_id") == callback_write_id:
                        item.pop("callback_write_id", None)
                        return True
        return False

    def _private_outbound_echo_primary_reservation_active(self, message):
        reservation_id = str(getattr(message, "_wxbot_outbound_echo_reservation", "") or "").strip()
        if not reservation_id:
            return False
        with self._chat_merge_lock:
            return any(
                item.get("reservation_id") == reservation_id
                and item.get("primary_memory_writer")
                for echoes in self._private_outbound_echoes.values()
                for item in echoes or []
                if isinstance(item, dict)
            )

    def _schedule_private_outbound_echo_fallback(self, chat_name):
        if not getattr(getattr(self, "config", None), "memory_switch", False):
            return False
        self._ensure_message_runtime_state()
        if self._private_outbound_echo_fallback_stopped:
            return False
        with self._chat_merge_lock:
            if self._private_outbound_echo_fallback_stopped:
                return False
            now = time.time()
            pending_expirations = [
                float(item.get("expires_at", 0) or 0)
                for echoes in self._private_outbound_echoes.values()
                for item in echoes or []
                if isinstance(item, dict)
                and str(item.get("fallback_content") or "").strip()
                and not item.get("memory_persisted")
            ]
            if not pending_expirations:
                return False
            expires_at = min(pending_expirations)
            active = getattr(self, "_private_outbound_echo_fallback_timer", None)
            active_deadline = float(getattr(self, "_private_outbound_echo_fallback_deadline", 0) or 0)
            if active is not None and active.is_alive():
                if active_deadline and active_deadline <= expires_at + 0.1:
                    return True
                active.cancel()
            timer = threading.Timer(
                max(0.1, expires_at - now + 0.1),
                self._run_private_outbound_echo_fallback_timer,
            )
            timer.daemon = True
            self._private_outbound_echo_fallback_timer = timer
            self._private_outbound_echo_fallback_deadline = expires_at + 0.1
            timer.start()
        return True

    def _run_private_outbound_echo_fallback_timer(self):
        with self._chat_merge_lock:
            self._private_outbound_echo_fallback_timer = None
            self._private_outbound_echo_fallback_deadline = 0
            if self._private_outbound_echo_fallback_stopped:
                return
        self._flush_expired_private_outbound_echo_fallbacks()
        self._schedule_private_outbound_echo_fallback("")

    def _save_private_outbound_echo_fallback(self, chat_name, echo):
        content = str((echo or {}).get("fallback_content") or "").strip()
        if not content or not getattr(getattr(self, "config", None), "memory_switch", False):
            return False
        memory_manager = getattr(self, "memory_manager", None)
        save_message = getattr(memory_manager, "save_message", None)
        if not callable(save_message):
            return False
        try:
            save_message(
                chat_name=str(chat_name or "").strip(),
                sender="self",
                content=content,
                msg_type=str((echo or {}).get("type") or "unknown").strip() or "unknown",
                msg_attr="self",
                max_count=getattr(self.config, "memory_max_count", 1000),
            )
            return True
        except Exception as exc:
            log(level="WARNING", message=f"发送记录补写消息记忆失败: {exc}")
            return False

    def _flush_expired_private_outbound_echo_fallbacks(self, chat_name=None, *, now=None):
        self._ensure_message_runtime_state()
        with self._private_outbound_history_write_lock:
            if self._private_outbound_echo_fallback_stopped:
                return 0
            return self._flush_expired_private_outbound_echo_fallbacks_locked(chat_name, now=now)

    def _flush_expired_private_outbound_echo_fallbacks_locked(self, chat_name=None, *, now=None):
        self._ensure_message_runtime_state()
        now = time.time() if now is None else float(now)
        target = str(chat_name or "").strip()
        pending_writes = []
        with self._chat_merge_lock:
            names = [target] if target else list(self._private_outbound_echoes)
            for name in names:
                next_echoes = []
                for echo in self._private_outbound_echoes.get(name, []):
                    if not isinstance(echo, dict):
                        continue
                    if echo.get("fallback_write_id"):
                        next_echoes.append(echo)
                        continue
                    if echo.get("callback_write_id"):
                        next_echoes.append(echo)
                        continue
                    if float(echo.get("expires_at", 0) or 0) >= now:
                        next_echoes.append(echo)
                        continue
                    fallback_content = str(echo.get("fallback_content") or "").strip()
                    if not fallback_content or echo.get("memory_persisted"):
                        continue
                    write_id = uuid.uuid4().hex
                    echo["fallback_write_id"] = write_id
                    pending_writes.append((name, dict(echo), write_id))
                    next_echoes.append(echo)
                if next_echoes:
                    self._private_outbound_echoes[name] = next_echoes[-PRIVATE_OUTBOUND_ECHO_MAX_PER_CHAT:]
                else:
                    self._private_outbound_echoes.pop(name, None)
        saved = 0
        for name, echo, write_id in pending_writes:
            count = max(1, int(echo.get("remaining", 1) or 1))
            success = all(
                self._save_private_outbound_echo_fallback(name, echo)
                for _index in range(count)
            )
            with self._chat_merge_lock:
                current = next(
                    (
                        item
                        for item in self._private_outbound_echoes.get(name, [])
                        if item.get("echo_id") == echo.get("echo_id")
                        and item.get("fallback_write_id") == write_id
                    ),
                    None,
                )
                if current is None:
                    continue
                current.pop("fallback_write_id", None)
                if success:
                    saved += count
                    current["memory_persisted"] = True
                    current["fallback_content"] = ""
                    current["expires_at"] = now + 2
                else:
                    current.pop("reservation_id", None)
                    current["expires_at"] = now + PRIVATE_OUTBOUND_ECHO_UNKNOWN_SECONDS
        return saved

    def _finalize_private_outbound_echo_fallbacks_on_stop(self):
        self._ensure_message_runtime_state()
        with self._chat_merge_lock:
            self._private_outbound_echo_fallback_stopped = True
            timer = getattr(self, "_private_outbound_echo_fallback_timer", None)
            if timer is not None:
                timer.cancel()
            self._private_outbound_echo_fallback_timer = None
            self._private_outbound_echo_fallback_deadline = 0
        acquired = self._private_outbound_history_write_lock.acquire(
            timeout=PRIVATE_OUTBOUND_HISTORY_STOP_WAIT_SECONDS
        )
        if not acquired:
            log(level="WARNING", message="停止机器人等待发送记录写入超时，已放弃旧实例兜底并继续退出")
            return False
        try:
            with self._chat_merge_lock:
                self._private_outbound_echoes = {}
        finally:
            self._private_outbound_history_write_lock.release()
        with self._chat_merge_lock:
            timer = getattr(self, "_private_outbound_echo_fallback_timer", None)
            if timer is not None:
                timer.cancel()
            self._private_outbound_echo_fallback_timer = None
            self._private_outbound_echo_fallback_deadline = 0
        return True

    def _has_pending_private_outbound_echoes(self):
        self._ensure_message_runtime_state()
        now = time.time()
        self._flush_expired_private_outbound_echo_fallbacks(now=now)
        pending = False
        with self._chat_merge_lock:
            next_echoes = {}
            for name, echoes in list((self._private_outbound_echoes or {}).items()):
                kept = []
                for item in echoes or []:
                    if not isinstance(item, dict):
                        continue
                    try:
                        expires_at = float(item.get("expires_at", 0) or 0)
                        remaining = int(item.get("remaining", 1))
                    except (TypeError, ValueError):
                        continue
                    claimed = bool(
                        item.get("reservation_id")
                        or item.get("callback_write_id")
                        or item.get("fallback_write_id")
                    )
                    if expires_at >= now and (remaining > 0 or claimed):
                        kept.append(item)
                if kept:
                    next_echoes[name] = kept[-PRIVATE_OUTBOUND_ECHO_MAX_PER_CHAT:]
                    pending = True
            self._private_outbound_echoes = next_echoes
        return pending

    def _remember_persisted_private_reply_echo(self, chat_name, content):
        return self._remember_private_outbound_echo(
            chat_name,
            "text",
            content,
            source="private_ai_reply",
            memory_persisted=True,
        )

    def _consume_persisted_private_reply_echo(self, chat_name, content):
        return self._consume_private_outbound_echo(
            chat_name,
            content=content,
            msg_type="text",
        )

    def _save_private_reply_memory_message(self, chat, content, *, msg_type="text"):
        text = str(content or "").strip()
        if str(msg_type or "").strip().lower() == "voice":
            text = strip_voice_duration_metadata(text)
        if not text:
            return False
        if not getattr(getattr(self, "config", None), "memory_switch", False):
            return False
        memory_manager = getattr(self, "memory_manager", None)
        save_message = getattr(memory_manager, "save_message", None)
        if not callable(save_message):
            return False
        try:
            save_message(
                chat_name=str(getattr(chat, "who", "") or "").strip(),
                sender="self",
                content=text,
                msg_type=str(msg_type or "text").strip() or "text",
                msg_attr="self",
                max_count=getattr(self.config, "memory_max_count", 1000),
            )
            return True
        except Exception as exc:
            log(level="WARNING", message=f"写入机器人回复记忆失败: {exc}")
            return False

    @staticmethod
    def _private_reply_send_allows_memory_save(result):
        return ReplyCountStore.was_send_success(result)

    def _private_reply_runtime_turn_list(self, chat_name):
        self._ensure_message_runtime_state()
        turns = self._private_reply_runtime_turns.get(chat_name)
        if not isinstance(turns, list):
            turns = []
            self._private_reply_runtime_turns[chat_name] = turns
        return turns

    def _cleanup_private_reply_runtime_turns(self, chat_name):
        turns = self._private_reply_runtime_turn_list(chat_name)
        kept = []
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            parts = [str(part or "").strip() for part in turn.get("parts", []) if str(part or "").strip()]
            echoed_count = max(0, min(int(turn.get("echoed_count", 0) or 0), len(parts)))
            active = bool(turn.get("active", False))
            if not parts and not active:
                continue
            if not active and echoed_count >= len(parts):
                continue
            turn["parts"] = parts
            turn["echoed_count"] = echoed_count
            turn["active"] = active
            kept.append(turn)
        self._private_reply_runtime_turns[chat_name] = kept[-5:]

    def _begin_private_reply_runtime_turn(self, chat_name):
        turn = {"parts": [], "echoed_count": 0, "active": True}
        self._private_reply_runtime_turn_list(chat_name).append(turn)
        self._cleanup_private_reply_runtime_turns(chat_name)
        return turn

    def _append_private_reply_runtime_part(self, turn, part):
        if not isinstance(turn, dict):
            return
        text = str(part or "").strip()
        if not text:
            return
        turn.setdefault("parts", []).append(text)

    def _finish_private_reply_runtime_turn(self, chat_name, turn):
        if isinstance(turn, dict):
            turn["active"] = False
        self._cleanup_private_reply_runtime_turns(chat_name)

    def _consume_private_reply_runtime_echo(self, chat_name, content):
        text = str(content or "").strip()
        if not text:
            return False
        matched = False
        for turn in self._private_reply_runtime_turn_list(chat_name):
            parts = turn.get("parts", []) or []
            echoed_count = int(turn.get("echoed_count", 0) or 0)
            if echoed_count >= len(parts):
                continue
            if str(parts[echoed_count] or "").strip() != text:
                continue
            turn["echoed_count"] = echoed_count + 1
            matched = True
            break
        self._cleanup_private_reply_runtime_turns(chat_name)
        return matched

    def _matches_private_reply_runtime_echo(self, chat_name, content):
        text = str(content or "").strip()
        if not text:
            return False
        for turn in self._private_reply_runtime_turn_list(chat_name):
            parts = turn.get("parts", []) or []
            echoed_count = int(turn.get("echoed_count", 0) or 0)
            if echoed_count < len(parts) and str(parts[echoed_count] or "").strip() == text:
                return True
        return False

    def _runtime_private_reply_history(self, chat_name):
        history = []
        for turn in self._private_reply_runtime_turn_list(chat_name):
            parts = [str(part or "").strip() for part in turn.get("parts", []) if str(part or "").strip()]
            echoed_count = max(0, min(int(turn.get("echoed_count", 0) or 0), len(parts)))
            remaining_parts = parts[echoed_count:]
            if not remaining_parts:
                continue
            history.append({
                "attr": "self",
                "sender": "self",
                "content": "\n".join(remaining_parts),
            })
        return history

    @staticmethod
    def _normalize_visual_image_paths(image_paths):
        return [
            str(path or "").strip()
            for path in (image_paths or [])
            if str(path or "").strip()
        ][:MAX_MERGED_PRIVATE_IMAGES]

    @staticmethod
    def _normalize_visual_note_slots(image_paths, visual_notes):
        notes = [str(note or "").strip() for note in (visual_notes or [])]
        return [
            notes[index] if index < len(notes) else ""
            for index, _path in enumerate(image_paths or [])
        ]

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

    def _set_pending_visual_context(self, chat_name, image_paths, *, visual_notes=None, append=False):
        self._ensure_message_runtime_state()
        normalized_paths = self._normalize_visual_image_paths(image_paths)
        if not normalized_paths:
            self._clear_pending_visual_context(chat_name)
            return None
        normalized_notes = self._normalize_visual_note_slots(normalized_paths, visual_notes)
        if append:
            existing = self._get_pending_visual_context(chat_name)
            if existing:
                existing_paths = self._normalize_visual_image_paths(existing.get("image_paths"))
                existing_notes = self._normalize_visual_note_slots(existing_paths, existing.get("visual_notes"))
                normalized_paths = self._normalize_visual_image_paths(existing_paths + normalized_paths)
                normalized_notes = self._normalize_visual_note_slots(
                    normalized_paths,
                    existing_notes + normalized_notes,
                )
        context = {
            "image_paths": normalized_paths,
            "visual_notes": normalized_notes,
            "expires_at": time.time() + PENDING_VISUAL_CONTEXT_TTL_SECONDS,
            "resolved": False,
        }
        with self._chat_merge_lock:
            self._pending_visual_contexts[chat_name] = context
        return dict(context)

    def _get_pending_visual_context(self, chat_name):
        self._ensure_message_runtime_state()
        with self._chat_merge_lock:
            context = dict(self._pending_visual_contexts.get(chat_name) or {})
            if not context:
                return None
            if context.get("resolved"):
                self._pending_visual_contexts.pop(chat_name, None)
                return None
            if float(context.get("expires_at", 0) or 0) < time.time():
                self._pending_visual_contexts.pop(chat_name, None)
                return None
            context["image_paths"] = self._normalize_visual_image_paths(context.get("image_paths"))
            if not context["image_paths"]:
                self._pending_visual_contexts.pop(chat_name, None)
                return None
            context["visual_notes"] = self._normalize_visual_note_slots(
                context["image_paths"],
                context.get("visual_notes"),
            )
            return context

    def _clear_pending_visual_context(self, chat_name):
        self._ensure_message_runtime_state()
        with self._chat_merge_lock:
            self._pending_visual_contexts.pop(chat_name, None)

    def _pending_visual_context_ready_to_clear(self, chat_name):
        context = self._get_pending_visual_context(chat_name)
        if not context:
            return True
        image_paths = self._normalize_visual_image_paths(context.get("image_paths"))
        visual_notes = self._normalize_visual_note_slots(image_paths, context.get("visual_notes"))
        return bool(visual_notes and any(visual_notes))

    def _remember_visual_notes(self, chat_name, image_paths, visual_notes):
        normalized_paths = self._normalize_visual_image_paths(image_paths)
        normalized_notes = self._normalize_visual_note_slots(normalized_paths, visual_notes)
        if not normalized_paths or not any(normalized_notes):
            return False
        updated = False
        memory_manager = getattr(self, "memory_manager", None)
        attach_notes = getattr(memory_manager, "attach_visual_notes", None)
        if callable(attach_notes):
            try:
                updated = bool(attach_notes(chat_name, normalized_paths, normalized_notes)) or updated
            except Exception as exc:
                log(level="WARNING", message=f"回写图片摘要到聊天记录失败: {exc}")
        self._ensure_message_runtime_state()
        with self._chat_merge_lock:
            context = dict(getattr(self, "_pending_visual_contexts", {}).get(chat_name) or {})
            context_paths = self._normalize_visual_image_paths(context.get("image_paths"))
            if context_paths and context_paths == normalized_paths:
                existing_notes = self._normalize_visual_note_slots(context_paths, context.get("visual_notes"))
                if existing_notes != normalized_notes:
                    context["visual_notes"] = list(normalized_notes)
                    self._pending_visual_contexts[chat_name] = context
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

    def _active_tts_config(self, user_name=""):
        tts_index = getattr(self.config, 'tts_index', 0)
        if user_name and self._is_private_whitelist_user(user_name):
            chat_tts_map = getattr(self.config, 'chat_tts_map', {}) or {}
            if isinstance(chat_tts_map, dict) and user_name in chat_tts_map:
                try:
                    tts_index = int(chat_tts_map.get(user_name))
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

    def _try_send_voice_reply(
        self,
        chat,
        clean_reply,
        *,
        state_key,
        cooldown_minutes,
        limit_count,
        limit_hours,
        context_text="",
        section_id="",
        expected_sequence=None,
        unanswered_record_id="",
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
            cooldown_minutes=cooldown_minutes,
            limit_count=limit_count,
            limit_hours=limit_hours,
        ):
            if not limiter._passes_cooldown(state_key, now=now, cooldown_minutes=cooldown_minutes):
                return False
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
        send_started = False
        try:
            tts_cfg = self._active_tts_config(
                getattr(chat, "who", "") if str(state_key or "").startswith("private:") else ""
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
            send_audio = getattr(chat, 'SendAudio', None)
            if not callable(send_audio):
                return False
            release_wechat_lock = wechat_ui_actions.try_acquire(self)
            if not release_wechat_lock:
                return False
            try:
                self._mark_unanswered_send_started(unanswered_record_id)
                send_started = True
                echo_group_id = ""
                if str(state_key or "").startswith("private:"):
                    echo_group_id = self._reserve_private_outbound_echo_for_send(
                        chat.who,
                        "voice",
                        source="private_ai_reply",
                        fallback_content=clean_reply,
                        primary_memory_writer=True,
                    )
                result = send_audio(
                    filepath=str(audio_path),
                    duration=None,
                    conversation_version=expected_sequence or 0,
                )
            finally:
                release_wechat_lock()
            if ReplyCountStore.was_send_success(result) or self._private_reply_send_allows_memory_save(result):
                if chat.who == getattr(self.config, "cmd", ""):
                    takeover_runtime.remember_admin_echo_message(self, clean_reply)
                if self._save_private_reply_memory_message(chat, clean_reply, msg_type="voice"):
                    self._mark_private_outbound_echo_group_persisted(echo_group_id)
                else:
                    self._schedule_private_outbound_echo_fallback("")
                limiter.mark_sent(state_key, now=now, limit_hours=limit_hours)
                return True
            if result is None:
                self._schedule_private_outbound_echo_fallback("")
            else:
                self._mark_private_outbound_echo_group_reported_failed(echo_group_id)
        except Exception as exc:
            if send_started:
                self._schedule_private_outbound_echo_fallback("")
                self._mark_unanswered_uncertain(unanswered_record_id)
                log(
                    level="ERROR",
                    message=(
                        "语音回复已进入微信提交阶段但结果未知，已禁止自动降级文字："
                        f"{self._format_voice_reply_error(exc)}"
                    ),
                )
                raise
            log(
                level="WARNING",
                message=f"语音回复失败，已降级文字发送：{self._format_voice_reply_error(exc)}",
            )
        finally:
            self._remove_temp_audio_file(audio_path)
        return False

    def _send_private_ai_reply_parts(self, chat, parts, *, expected_sequence=None):
        send_success = False
        result = True
        if not self._private_reply_can_continue(chat, expected_sequence=expected_sequence):
            return False, True
        target = str(getattr(chat, "who", "") or "").strip()
        release_wechat_lock = wechat_ui_actions.try_acquire(self)
        if not release_wechat_lock:
            queued = self._queue_text_reply_until_target_verified(
                target,
                parts,
                source="private_ai_reply",
                expected_sequence=expected_sequence,
            )
            log(level="INFO", message=f"私聊 {target} 微信 UI 正忙，AI 回复已进入延迟发送队列")
            return False, queued
        try:
            send_chat = self._verified_send_chat(target, chat)
            if send_chat is None and target:
                send_chat = self._ensure_target_listen_chat_for_send(target)
            if send_chat is None:
                queued = self._queue_text_reply_until_target_verified(
                    target,
                    parts,
                    source="private_ai_reply",
                    expected_sequence=expected_sequence,
                )
                log(level="WARNING", message=f"私聊 {target} 发送前未能确认目标子窗口，已进入延迟发送队列，避免回错人")
                return False, queued
            chat = send_chat
            runtime_turn = self._begin_private_reply_runtime_turn(chat.who)
            try:
                with self._get_chat_send_lock(chat.who):
                    last_index = max(0, len(parts) - 1)
                    for idx, part in enumerate(parts):
                        if not self._private_reply_can_continue(chat, expected_sequence=expected_sequence):
                            break
                        self._human_delay_for_reply_part(
                            part_text=part,
                            split_continuation=(idx > 0),
                            is_last=(idx == last_index),
                            delay_enabled=bool(
                                getattr(getattr(self, "config", None), "chat_split_reply_switch", False)
                                and getattr(getattr(self, "config", None), "chat_split_reply_delay_switch", True)
                            ),
                        )
                        if not self._private_reply_can_continue(chat, expected_sequence=expected_sequence):
                            break
                        if len(part) >= LONG_REPLY_SEGMENT_CHARS:
                            segments = list(self.config.split_long_text(part))
                        else:
                            segments = [part]
                        last_segment_index = max(0, len(segments) - 1)
                        for segment_index, segment in enumerate(segments):
                            if not self._private_reply_can_continue(chat, expected_sequence=expected_sequence):
                                break
                            if segment_index > 0:
                                self._human_delay_for_reply_part(
                                    part_text=segment,
                                    split_continuation=True,
                                    is_last=(segment_index == last_segment_index),
                                    delay_enabled=bool(
                                        getattr(getattr(self, "config", None), "chat_split_reply_switch", False)
                                        and getattr(getattr(self, "config", None), "chat_split_reply_delay_switch", True)
                                    ),
                                )
                                if not self._private_reply_can_continue(chat, expected_sequence=expected_sequence):
                                    break
                            self._mark_unanswered_send_started()
                            echo_group_id = uuid.uuid4().hex
                            self._remember_private_outbound_echo(
                                chat.who,
                                "text",
                                segment,
                                source="private_ai_reply",
                                fallback_content=segment,
                                echo_group_id=echo_group_id,
                                primary_memory_writer=True,
                            )
                            try:
                                if isinstance(chat, OwnedChat):
                                    send_result = chat.SendMsg(
                                        segment,
                                        conversation_version=expected_sequence or 0,
                                    )
                                else:
                                    send_result = chat.SendMsg(segment)
                            except wechat_ui_actions.IntentCancelled:
                                self._discard_private_outbound_echo_group(echo_group_id)
                                break
                            except Exception:
                                self._settle_private_outbound_echo_send(echo_group_id, None)
                                raise
                            result = send_result
                            if chat.who == getattr(self.config, "cmd", ""):
                                takeover_runtime.remember_admin_echo_message(self, segment)
                            current_success = ReplyCountStore.was_send_success(send_result)
                            if current_success or self._private_reply_send_allows_memory_save(send_result):
                                if self._save_private_reply_memory_message(chat, segment):
                                    self._mark_private_outbound_echo_group_persisted(echo_group_id)
                                else:
                                    self._schedule_private_outbound_echo_fallback("")
                                    self._append_private_reply_runtime_part(runtime_turn, segment)
                            elif send_result is None:
                                self._release_private_outbound_echo_group_to_callback(echo_group_id)
                                self._append_private_reply_runtime_part(runtime_turn, segment)
                            else:
                                self._mark_private_outbound_echo_group_reported_failed(echo_group_id)
                            send_success = send_success or current_success
            finally:
                self._finish_private_reply_runtime_turn(chat.who, runtime_turn)
        finally:
            release_wechat_lock()
        return send_success, result

    def _send_keyword_reply_actions(self, chat, actions, *, at=None, delivery_id=""):
        send_success = False
        result = True
        actions = list(actions or [])
        if not actions:
            log(level="ERROR", message=f"关键词回复命中但没有可发送内容，已停止处理：{chat.who}")
            return False, False
        is_group_chat = getattr(chat, "chat_type", "") == "group" or chat.who in getattr(self.config, "group", [])
        private_chat = not is_group_chat and chat.who != getattr(self.config, "cmd", "")
        if private_chat and not self._private_reply_can_continue(chat):
            return False, True
        release_wechat_lock = None
        if private_chat:
            release_wechat_lock = wechat_ui_actions.try_acquire(self)
            if not release_wechat_lock:
                queued = self._queue_keyword_reply_until_target_verified(chat.who, actions)
                log(level="INFO", message=f"关键词回复 {chat.who} 微信 UI 正忙，已进入延迟发送队列")
                return False, queued
            send_chat = self._verified_send_chat(chat.who, chat) or self._ensure_target_listen_chat_for_send(chat.who)
            if send_chat is None:
                queued = self._queue_keyword_reply_until_target_verified(chat.who, actions)
                log(level="WARNING", message=f"关键词回复 {chat.who} 发送前未能确认目标子窗口，已进入延迟发送队列，避免回错人")
                if release_wechat_lock:
                    release_wechat_lock()
                return False, queued
            chat = send_chat
        if isinstance(chat, OwnedChat):
            sent_count = 0
            try:
                expected_sequence = self._get_private_message_sequence(chat.who) if private_chat else 0
                task_key, task_version = self._config_ui_task_guard("keyword")
                for index, action in enumerate(actions):
                    if self.is_stop_requested() or (private_chat and not self._private_reply_can_continue(chat)):
                        break
                    if index > 0:
                        self._inter_message_delay_or_stop()
                    if self.is_stop_requested() or (private_chat and not self._private_reply_can_continue(chat)):
                        break
                    action_type = str(action.get("type") or "").strip().lower()
                    if action_type == "text":
                        content = str(action.get("content") or "").strip()
                        if not content:
                            continue
                        prepared_action = {"type": "text", "text": content, "at": str(at or "")}
                    else:
                        path = str(action.get("path") or "").strip()
                        if not path or not os.path.isfile(path):
                            log(level="ERROR", message=f"关键词回复文件不存在，已跳过：{path}")
                            continue
                        prepared_action = {"type": "file", "path": path}
                    echo_group_id = ""
                    if private_chat:
                        echo_group_id = self._reserve_private_outbound_echo_for_action(
                            chat.who,
                            prepared_action,
                            source="keyword_reply",
                        )
                    try:
                        results = chat.SendActions(
                            [prepared_action],
                            conversation_version=expected_sequence,
                            task_key=task_key,
                            task_version=task_version,
                            delivery_id=f"{delivery_id}:{index}",
                        )
                    except wechat_ui_actions.IntentCancelled:
                        self._discard_private_outbound_echo_group(echo_group_id)
                        break
                    except Exception:
                        self._settle_private_outbound_echo_send(echo_group_id, None)
                        raise
                    results = list(results or []) if isinstance(results, (list, tuple)) else [results]
                    action_result = results[-1] if results else True
                    result = action_result
                    sent_count += 1
                    if private_chat:
                        self._settle_private_outbound_echo_send(echo_group_id, action_result)
                    send_success = send_success or ReplyCountStore.was_send_success(action_result)
            finally:
                if release_wechat_lock:
                    release_wechat_lock()
            if not send_success:
                log(level="ERROR", message=f"关键词回复命中但未成功发送任何内容，已停止处理：{chat.who}")
                return False, False
            log(level="SUCCESS", message=f"关键词回复成功：{chat.who}，发送 {sent_count} 条")
            return send_success, result
        try:
            with self._get_chat_send_lock(chat.who):
                for index, action in enumerate(actions):
                    if self.is_stop_requested():
                        log(message=f"{chat.who} 机器人正在停止，停止发送关键词回复剩余内容")
                        break
                    if private_chat and not self._private_reply_can_continue(chat):
                        break
                    action_type = str(action.get("type") or "").strip().lower()
                    if index > 0:
                        self._inter_message_delay_or_stop()
                    if self.is_stop_requested():
                        log(message=f"{chat.who} 机器人正在停止，停止发送关键词回复剩余内容")
                        break
                    if private_chat and not self._private_reply_can_continue(chat):
                        break
                    if action_type == "text":
                        content = str(action.get("content") or "").strip()
                        if not content:
                            continue
                        if private_chat:
                            result = self._run_private_outbound_echo_send(
                                chat.who,
                                {"type": "text", "text": content},
                                lambda: chat.SendMsg(msg=content, at=at) if at else chat.SendMsg(msg=content),
                                source="keyword_reply",
                            )
                        elif at:
                            result = chat.SendMsg(msg=content, at=at)
                        else:
                            result = chat.SendMsg(msg=content)
                    else:
                        path = str(action.get("path") or "").strip()
                        if not path or not os.path.isfile(path):
                            log(level="ERROR", message=f"关键词回复文件不存在，已跳过：{path}")
                            continue
                        if private_chat:
                            result = self._run_private_outbound_echo_send(
                                chat.who,
                                {"type": "file", "path": path},
                                lambda: chat.SendFiles(filepath=path),
                                source="keyword_reply",
                            )
                        else:
                            result = chat.SendFiles(filepath=path)
                    send_success = send_success or ReplyCountStore.was_send_success(result)
        finally:
            if release_wechat_lock:
                release_wechat_lock()
        if not send_success:
            log(level="ERROR", message=f"关键词回复命中但未成功发送任何内容，已停止处理：{chat.who}")
            return False, False
        log(
            level="SUCCESS",
            message=f"关键词回复成功：{chat.who}，发送 {len(actions)} 条",
        )
        return send_success, result

    def _get_reply_count_key(self, chat, message=None):
        """获取回复计数器 key；当前 wxautox4 可用稳定字段有限，先集中使用 chat.who。"""
        return str(getattr(chat, 'who', '') or '').strip()

    def _get_group_reply_once_key(self, chat, message=None):
        group_name = str(getattr(chat, 'who', '') or '').strip()
        sender = str(getattr(message, 'sender', '') or '').strip()
        if not group_name:
            return ""
        return f"group:{group_name}:{sender}" if sender else f"group:{group_name}"

    def _get_text_reply_limit_count(self, user_name):
        """获取私聊用户的回复轮数上限；当前统一使用全局配置。"""
        return self.config.text_reply_limit_count

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

    def _get_model_context_history(self, chat_who):
        try:
            count = max(0, int(getattr(self.config, 'memory_context_count', 0) or 0))
        except Exception:
            count = 0
        if count <= 0:
            return []
        try:
            raw_history = []
            if self.memory_manager:
                raw_limit = self._memory_context_raw_limit(count)
                raw_history = self.memory_manager.get_messages(chat_who, raw_limit) or []
            runtime_history = self._runtime_private_reply_history(chat_who)
            merged_history = list(raw_history) + runtime_history
            history = build_model_visible_history(
                merged_history,
                message_limit=count,
            )
            return history
        except Exception as e:
            log(level="WARNING", message=f"读取AI上下文失败: {e}")
            return []

    def _text_reply_limit_history(self, chat_who):
        if not self.memory_manager:
            return []
        try:
            count = max(0, int(getattr(self.config, 'memory_context_count', 0) or 0))
        except Exception:
            count = 0
        if count <= 0:
            return []
        try:
            raw_limit = self._memory_context_raw_limit(count)
            raw_history = self.memory_manager.get_messages(chat_who, raw_limit) or []
            history = build_model_visible_history(
                raw_history,
                message_limit=count,
            )
            return history
        except Exception as e:
            log(level="WARNING", message=f"读取轮数超限结束语上下文失败: {e}")
            return []

    def _build_text_reply_limit_ai_prompt(self, chat_name):
        system = getattr(self, "prompt_system", None)
        if system is None:
            system = self._init_prompt_system()
        return system.render_template_prompt(
            CLOSING_REPLY_PROMPT_FILE,
            chat_name,
            chat_type="private",
        )

    def _generate_text_reply_limit_reply(self, chat, message):
        prompt = self._build_text_reply_limit_ai_prompt(chat.who)
        history = self._text_reply_limit_history(chat.who)
        content = str(getattr(message, 'content', '') or '').strip()
        reply = self._get_chat_api(chat.who).chat(content, prompt=prompt, history=history)
        return str(reply or '').strip()

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

    def _check_text_reply_limit(self, chat, user_key, message=None):
        """检查并处理私聊回复轮数超限；返回 (是否已处理, 发送结果)。"""
        if not self.config.text_reply_limit_switch or not user_key:
            return False, True
        max_round = self._get_text_reply_limit_count(user_key)
        limit_hours = getattr(self.config, "text_reply_limit_hours", 24)
        if self.reply_count_store.can_consume(
            user_key,
            limit_count=max_round,
            limit_hours=limit_hours,
        ):
            return False, True
        user_data = self.reply_count_store.get_user(user_key, limit_hours=limit_hours)
        if self.config.text_reply_limit_reply_once and user_data.get("limit_notified"):
            return True, True
        if self._should_log_text_reply_limit_warning(
            user_key,
            user_data,
            limit_count=max_round,
            limit_hours=limit_hours,
        ):
            log(
                level="WARNING",
                message=f"私聊 {chat.who} 触发回复上限：{limit_hours} 小时最多 {max_round} 轮",
            )
        reply_text = ""
        if getattr(self.config, 'text_reply_limit_ai_reply', False):
            try:
                log(message=f"私聊 {chat.who} 触发轮数超限，使用 AI 自动生成结束语")
                reply_text = self._generate_text_reply_limit_reply(chat, message)
                if is_api_error_reply(reply_text):
                    log(level="WARNING", message="轮数超限结束语生成遇到 API 错误，转入接口报错回复策略")
                    if getattr(self.config, "api_error_reply_once", False):
                        api_user_data = self._reply_once_user_data(user_key)
                        if api_user_data.get("api_err_notified"):
                            return True, True
                    parts = self._api_error_reply_parts()
                    send_success, result = self._send_private_ai_reply_parts(chat, parts)
                    if send_success:
                        self._record_reply_metric_success(chat.who, chat_type="private")
                        if getattr(self.config, "api_error_reply_once", False):
                            self.reply_count_store.mark_api_err_notified(user_key)
                    return True, result
            except Exception as e:
                log(level="WARNING", message=f"轮数超限结束语生成失败，已静默跳过: {e}")
                reply_text = ""
        else:
            reply_text = str(getattr(self.config, 'text_reply_limit_reply', '') or '').strip()
        if not reply_text:
            return True, True

        send_success, result = self._send_private_ai_reply_parts(chat, [reply_text])
        if send_success:
            self._record_reply_metric_success(chat.who, chat_type="private")
            if self.config.text_reply_limit_reply_once:
                self.reply_count_store.mark_limit_notified(user_key)
        return True, result

    def _check_text_reply_limit_runtime(self, chat, user_key, message=None):
        return self._check_text_reply_limit(chat, user_key, message=message)

    def _is_custom_forward_source(self, chat_who):
        """判断某个会话是否是任意自定义转发规则的监听来源"""
        return is_custom_forward_source(self.config.custom_forward_list, chat_who)

    def _material_source_runtime_enabled(self):
        sources = [
            str(item or "").strip()
            for item in (getattr(self.config, "material_source_list", []) or [])
            if str(item or "").strip()
        ]
        return bool(sources)

    def _is_material_source_chat(self, chat_who):
        return bool(
            self._material_source_runtime_enabled()
            and is_material_source(getattr(self.config, 'material_source_list', []), chat_who)
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
                raw_history = list(memory_manager.get_messages(target, raw_limit) or [])
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
                raw_history = list(memory_manager.get_messages(target, raw_limit) or [])
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
            log(level="SUCCESS", message=f"[AI自动转发] {target_label} 已发送素材：{material_label}")
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
        if not self._is_material_source_chat(chat.who):
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
        if getattr(self.config, 'material_source_silent', True) and chat.who != self.config.cmd:
            log(message=f"[素材转发] 素材源 {chat.who} 非素材消息已静默跳过")
            return True
        return False

    # ----------------------------------------------------------
    # 管理员命令分发
    # ----------------------------------------------------------

    def process_command(self, chat, message):
        """
        解析并分发管理员指令。
        当前仅保留运行控制台相关指令；未命中时回退到管理员普通对话。

        :param chat:    管理员聊天窗口子对象
        :param message: 消息对象
        :return:        操作结果
        """
        result = dispatch_admin_command(self, chat, message)
        if result is not None:
            self._mark_message_skip_memory(message)
            return result
        if getattr(message, "attr", None) == "self":
            workspace_result = takeover_runtime.route_admin_plain_message(self, chat, message)
            if workspace_result is not None:
                self._mark_message_skip_memory(message)
                return workspace_result
        if getattr(message, "attr", None) != "self":
            return self._enqueue_private_message_for_ai(chat, message)
        return True

    def _load_admin_moments_draft(self):
        path = getattr(self, "_moments_draft_file", "")
        if not path:
            return None
        return load_active_draft(path)

    def _save_admin_moments_draft(self, draft):
        path = getattr(self, "_moments_draft_file", "")
        if not path:
            return draft
        save_active_draft(path, draft)
        return draft

    def _load_admin_forward_draft(self):
        path = getattr(self, "_forward_draft_file", "")
        if not path:
            return None
        return load_active_draft(path)

    def _save_admin_forward_draft(self, draft):
        path = getattr(self, "_forward_draft_file", "")
        if not path:
            return draft
        save_active_draft(path, draft)
        return draft

    def start_admin_moments_draft(self, chat):
        draft = self._load_admin_moments_draft()
        if admin_moments_flow.is_active_draft(draft):
            return chat.SendMsg("当前已有未完成的发圈草稿，发送 /取消发圈 后可重新开始")
        draft = create_empty_draft(source="admin_command")
        admin_moments_flow.arm_auto_cancel(draft)
        self._save_admin_moments_draft(draft)
        return chat.SendMsg(admin_moments_flow.start_prompt())

    def start_admin_forward_draft(self, chat):
        draft = self._load_admin_forward_draft()
        if admin_forward_flow.is_active_draft(draft):
            return chat.SendMsg("当前已有未完成的转发任务，完成或取消后再重新开始")
        draft = {
            "draft_id": f"forward_{uuid.uuid4().hex[:8]}",
            "status": "waiting_material",
            "source": "admin_command",
        }
        admin_forward_flow.arm_auto_cancel(draft)
        self._save_admin_forward_draft(draft)
        return chat.SendMsg(admin_forward_flow.start_prompt())

    def cancel_admin_forward_draft(self, chat, *, message="这次转发任务已取消"):
        path = getattr(self, "_forward_draft_file", "")
        if path:
            clear_active_draft(path)
        return chat.SendMsg(message)

    def cancel_admin_moments_draft(self, chat):
        draft = self._load_admin_moments_draft()
        self._delete_admin_moments_managed_uploads((draft or {}).get("images") or [])
        path = getattr(self, "_moments_draft_file", "")
        if path:
            clear_active_draft(path)
        return chat.SendMsg("这次发圈任务已取消")

    def _copy_admin_moments_image_to_uploads(self, draft, image_path):
        data_dir = self._task_storage_data_dir()
        wx_id = self._task_storage_wx_id()
        if not data_dir or not wx_id:
            return image_path
        return copy_moments_admin_upload(
            image_path,
            data_dir=data_dir,
            wx_id=wx_id,
            draft_id=(draft or {}).get("draft_id") or "admin",
        )

    def _delete_admin_moments_managed_uploads(self, images):
        data_dir = self._task_storage_data_dir()
        wx_id = self._task_storage_wx_id()
        if not data_dir or not wx_id:
            return 0
        return delete_managed_moments_uploads(images, data_dir=data_dir, wx_id=wx_id)

    def _get_admin_moments_target_chat(self, chat=None):
        if chat is not None:
            return chat
        if getattr(self, "wx", None) and hasattr(self.wx, "ChatWith"):
            try:
                with wechat_ui_actions.hold(self):
                    target = self.wx.ChatWith(who=self.config.cmd)
                if target is not None:
                    return target
            except Exception:
                pass
        return SimpleNamespace(SendMsg=lambda message: message)

    def _get_admin_moments_raw_text(self, draft):
        return "\n".join(
            str(item or "").strip()
            for item in (draft or {}).get("texts", [])
            if str(item or "").strip()
        ).strip() or "（未提供）"

    def _admin_forward_tag_options(self):
        directory, _directory_file, _wx_id = self._load_contact_profiles_directory()
        return admin_forward_flow.top_contact_tags(directory, limit=9)

    def _store_admin_forward_material(self, chat, message):
        materials, entry, material_id = collect_material_source_message(
            self._load_material_outreach_materials(),
            chat.who,
            message,
            material_id_factory=lambda: f"mat_{uuid.uuid4().hex}",
            limit_map=getattr(self.config, "material_source_pool_limit_map", {}) or {},
        )
        if not entry:
            return None
        self._save_material_outreach_materials(materials)
        self._material_runtime_messages[material_id] = message
        return entry

    def _create_admin_forward_task(self, draft):
        selector = normalize_target_selector({
            "mode": "include" if draft.get("include_tags") else ("exclude" if draft.get("exclude_tags") else "all"),
            "base": "all_friends",
            "include_tags": list(draft.get("include_tags") or []),
            "exclude_tags": list(draft.get("exclude_tags") or []),
            "include_contact_keys": [],
            "exclude_contact_keys": [],
        })
        task = {
            "id": f"admin_forward_{int(time.time() * 1000)}",
            "name": "管理员转发任务",
            "enabled": True,
            "trigger_strategy": "fixed",
            "mode": "fixed",
            "start_at": str(draft.get("scheduled_at") or "").strip(),
            "fixed_material_id": str(draft.get("material_id") or "").strip(),
            "material_types": [str(draft.get("material_type") or "").strip()] if str(draft.get("material_type") or "").strip() else ["all"],
            "target_selector": selector,
            "preface_mode": "none",
            "preface_text": "",
            "preface_random_emojis": False,
            "ai_preface_goal": DEFAULT_AI_PREFACE_GOAL,
            "ai_preface_intensity": "",
            "ai_preface_extra_instruction": "",
            "ai_preface_failure_mode": "send_without_preface",
            "batch_material_strategy": "fixed",
        }
        task = normalize_fixed_task_schedule(task, default_time="08:00", start_at_key="start_at")
        task["repeat_type"] = "once"
        tasks = list(getattr(self.config, "material_outreach_list", []) or [])
        tasks.append(task)
        next_tasks = [dict(item) for item in tasks if isinstance(item, dict)]
        tasks_file = self._material_outreach_tasks_file(create_parent=True)
        save_json_list(tasks_file, next_tasks)
        self._set_runtime_task_list("material_outreach_list", next_tasks)
        if hasattr(self, "request_runtime_task_reload"):
            self.request_runtime_task_reload()
        return next_tasks[-1]

    def _build_admin_moments_generation_prompt(self, draft):
        text_block = self._get_admin_moments_raw_text(draft)
        system = getattr(self, "prompt_system", None)
        if system is None:
            system = self._init_prompt_system()
        return system.render_moments_caption_prompt(self.config.cmd, text_block)

    def _parse_admin_moments_candidates(self, raw_reply):
        return parse_moments_candidates(raw_reply, cleaner=sanitize_ai_output_text)

    def _resolve_admin_moments_api_index(self):
        api_configs = getattr(self.config, "api_configs", []) or []
        if not api_configs:
            return -1
        try:
            configured = int(getattr(self.config, "moments_api_index", 0))
        except (TypeError, ValueError):
            configured = 0
        if 0 <= configured < len(api_configs):
            return configured
        return 0

    def _generate_admin_moments_candidates(self, draft):
        api_index = self._resolve_admin_moments_api_index()
        if api_index < 0:
            raise RuntimeError("未找到可用的发朋友圈专用接口")
        api = self._get_other_api(api_index)
        prompt = self._build_admin_moments_generation_prompt(draft)
        raw_text = self._get_admin_moments_raw_text(draft)
        message_text = "请基于这次素材生成 3 条可直接发布的朋友圈文案候选。"
        if raw_text and raw_text != "（未提供）":
            message_text = f"{message_text}\n\n原始短文案：\n{raw_text}"
        images = [
            str(item or "").strip()
            for item in (draft or {}).get("images", [])
            if str(item or "").strip()
        ]
        if images:
            raw_reply = api.chat(
                message_text,
                prompt=prompt,
                history=[],
                stream=False,
                image_paths=images,
            )
        else:
            raw_reply = api.chat(
                message_text,
                prompt=prompt,
                history=[],
                stream=False,
            )
        return self._parse_admin_moments_candidates(raw_reply)

    def regenerate_admin_moments_draft(self, chat=None, trigger="manual"):
        draft = self._load_admin_moments_draft()
        target = self._get_admin_moments_target_chat(chat)
        if not draft or not draft_has_material(draft):
            return target.SendMsg("请先发送文案或图片，再生成朋友圈预览")
        try:
            candidates = self._generate_admin_moments_candidates(draft)
        except Exception as exc:
            log(level="ERROR", message=f"发圈预览生成失败：{exc}")
            return target.SendMsg("本次朋友圈文案生成失败，请检查发朋友圈专用接口状态、图片可读性，或稍后重试。")
        draft["status"] = "preview_ready"
        draft["generated_candidates"] = list(candidates or [])[:3]
        draft["preview_generated_at"] = datetime.now().replace(microsecond=0).isoformat()
        draft["auto_preview_deadline"] = ""
        draft["auto_cancel_deadline"] = ""
        draft["selected_candidate_index"] = 0
        self._save_admin_moments_draft(draft)
        return target.SendMsg(render_preview_reply(draft))

    def publish_admin_moments_draft(self, chat, candidate_index=None):
        draft = self._load_admin_moments_draft()
        candidates = [
            str(item or "").strip()
            for item in (draft or {}).get("generated_candidates", [])
            if str(item or "").strip()
        ]
        if not draft or draft.get("status") != "preview_ready" or not candidates:
            return chat.SendMsg("请先生成朋友圈预览，再选择要发布的文案序号")
        if candidate_index in (None, 0):
            return chat.SendMsg(admin_moments_flow.build_candidate_selection_reply(draft))
        try:
            task = moments_task_from_admin_draft(draft, candidate_index=candidate_index)
        except ValueError:
            return chat.SendMsg("当前只能发布第 1 到第 3 条预览文案，请重新选择")
        try:
            queued_task = queue_moments_task(task, mode="immediate")
            tasks = getattr(self.config, "moments_task_list", None)
            if not isinstance(tasks, list):
                tasks = []
            previous_tasks = list(tasks)
            next_tasks = list(tasks)
            next_tasks.append(queued_task)
            self._save_moments_task_definitions_only(next_tasks)
            try:
                self._save_moments_runtime_record(queued_task)
            except Exception:
                try:
                    self._save_moments_task_definitions_only(previous_tasks)
                except Exception as rollback_exc:
                    log(level="ERROR", message=f"朋友圈任务回滚任务定义失败：{rollback_exc}")
                raise
            self._set_runtime_task_list("moments_task_list", next_tasks)
            if hasattr(self, "request_runtime_task_reload"):
                try:
                    self.request_runtime_task_reload()
                except Exception as exc:
                    log(level="WARNING", message=f"管理员发圈任务运行中同步失败，将在下次刷新后生效：{exc}")
        except Exception as exc:
            log(level="ERROR", message=f"朋友圈任务加入待执行失败：{exc}")
            return chat.SendMsg("朋友圈任务加入待执行失败，请稍后重试")
        path = getattr(self, "_moments_draft_file", "")
        if path:
            clear_active_draft(path)
        return chat.SendMsg("发圈任务已创建，已加入待执行队列")

    def _handle_admin_forward_input(self, chat, message):
        if chat.who != getattr(self.config, "cmd", ""):
            return False
        draft = self._load_admin_forward_draft()
        if not admin_forward_flow.is_active_draft(draft):
            return False
        content = str(getattr(message, "content", "") or "").strip()
        if not content or content.startswith("/"):
            return False

        status = str(draft.get("status") or "").strip()
        if status == "waiting_material":
            entry = self._store_admin_forward_material(chat, message)
            if not entry:
                chat.SendMsg("请直接转发一条素材消息给我")
                return True
            draft["status"] = "waiting_target_scope"
            draft["material_id"] = str(entry.get("id") or "").strip()
            draft["material_type"] = str(entry.get("type_bucket") or entry.get("type") or "").strip()
            draft["material_type_label"] = material_type_label(draft["material_type"]) or "素材"
            draft["material_preview"] = str(entry.get("content_preview") or "").strip()
            draft["tag_options"] = self._admin_forward_tag_options()
            self._save_admin_forward_draft(draft)
            chat.SendMsg(admin_forward_flow.render_target_prompt(draft["tag_options"]))
            return True

        if status == "waiting_target_scope":
            parsed = admin_forward_flow.parse_target_scope(content, draft.get("tag_options") or [])
            if parsed is None:
                chat.SendMsg("请直接回复上面的编号")
                return True
            draft["target_mode"] = parsed["mode"]
            draft["include_tags"] = list(parsed["include_tags"])
            draft["exclude_tags"] = []
            if parsed["mode"] == "all":
                draft["status"] = "waiting_target_exclude"
                self._save_admin_forward_draft(draft)
                chat.SendMsg(admin_forward_flow.render_exclude_prompt(draft.get("tag_options") or []))
                return True
            draft["status"] = "waiting_delay"
            self._save_admin_forward_draft(draft)
            chat.SendMsg(admin_forward_flow.render_delay_prompt())
            return True

        if status == "waiting_target_exclude":
            exclude_tags = admin_forward_flow.parse_exclude_scope(content, draft.get("tag_options") or [])
            if exclude_tags is None:
                chat.SendMsg("请直接回复上面的编号")
                return True
            draft["exclude_tags"] = list(exclude_tags)
            draft["status"] = "waiting_delay"
            self._save_admin_forward_draft(draft)
            chat.SendMsg(admin_forward_flow.render_delay_prompt())
            return True

        if status == "waiting_delay":
            delay_minutes = admin_forward_flow.parse_delay_minutes(content)
            if delay_minutes is None:
                chat.SendMsg("请回复 1、2、3，或直接回复分钟数")
                return True
            scheduled_at = (datetime.now() + timedelta(minutes=delay_minutes)).replace(microsecond=0)
            draft["delay_minutes"] = delay_minutes
            draft["scheduled_at"] = scheduled_at.isoformat()
            draft["status"] = "confirming"
            self._save_admin_forward_draft(draft)
            chat.SendMsg(admin_forward_flow.build_confirmation_reply(draft))
            return True

        if status == "confirming":
            if content == "1":
                self._create_admin_forward_task(draft)
                self.cancel_admin_forward_draft(chat, message="转发任务已创建")
                return True
            if content == "0":
                self.cancel_admin_forward_draft(chat)
                return True
            chat.SendMsg("请回复 1 或 0")
            return True
        return False

    def _handle_admin_moments_input(self, chat, message):
        if chat.who != getattr(self.config, "cmd", ""):
            return False
        draft = self._load_admin_moments_draft()
        if not admin_moments_flow.is_active_draft(draft):
            return False
        content = str(getattr(message, "content", "") or "").strip()
        if not content or content.startswith("/"):
            return False
        if draft.get("status") == "confirming":
            if content == "1":
                candidate_index = int(draft.get("selected_candidate_index") or 0)
                if candidate_index <= 0:
                    chat.SendMsg("请先选择要发布的文案序号")
                    return True
                preview_draft = dict(draft)
                preview_draft["status"] = "preview_ready"
                self._save_admin_moments_draft(preview_draft)
                self.publish_admin_moments_draft(chat, candidate_index=candidate_index)
                return True
            if content == "0":
                self.cancel_admin_moments_draft(chat)
                return True
            chat.SendMsg(admin_moments_flow.invalid_confirm_prompt())
            return True
        if draft.get("status") == "preview_ready":
            if content in {"1", "2", "3"}:
                try:
                    selected = int(content)
                except ValueError:
                    selected = 0
                candidates = [
                    str(item or "").strip()
                    for item in (draft.get("generated_candidates") or [])
                    if str(item or "").strip()
                ]
                if 1 <= selected <= len(candidates):
                    admin_moments_flow.select_candidate_for_confirmation(draft, selected)
                    self._save_admin_moments_draft(draft)
                    chat.SendMsg(admin_moments_flow.build_confirmation_reply(draft))
                    return True
                chat.SendMsg("请选择 1 到 3 的文案序号")
                return True
        now = datetime.now()
        image_path = self._existing_local_image_path(content)
        try:
            if image_path:
                stored_image_path = self._copy_admin_moments_image_to_uploads(draft, image_path)
                append_draft_image(draft, stored_image_path, now=now)
            else:
                append_draft_text(draft, content, now=now)
        except ValueError as exc:
            if str(exc) == "最多只能收 9 张图片":
                chat.SendMsg(admin_moments_flow.image_limit_prompt())
                return True
            raise
        admin_moments_flow.clear_auto_cancel(draft)
        self._save_admin_moments_draft(draft)
        return True

    def _check_admin_moments_auto_preview(self, now=None):
        now = now or datetime.now()
        draft = self._load_admin_moments_draft()
        if not draft or draft.get("status") != "collecting":
            return None
        cancel_deadline = str(draft.get("auto_cancel_deadline") or "").strip()
        if cancel_deadline and not draft_has_material(draft):
            try:
                if now >= datetime.fromisoformat(cancel_deadline):
                    path = getattr(self, "_moments_draft_file", "")
                    if path:
                        clear_active_draft(path)
                    target = self._get_admin_moments_target_chat(None)
                    return target.SendMsg("这次发圈任务已超时取消")
            except ValueError:
                pass
        deadline = str(draft.get("auto_preview_deadline") or "").strip()
        if not deadline:
            return None
        try:
            if now < datetime.fromisoformat(deadline):
                return None
        except ValueError:
            return None
        return self.regenerate_admin_moments_draft(None, trigger="auto")

    def _check_admin_forward_timeout(self, now=None):
        now = now or datetime.now()
        draft = self._load_admin_forward_draft()
        if not draft or str(draft.get("status") or "").strip() != "waiting_material":
            return None
        deadline = str(draft.get("auto_cancel_deadline") or "").strip()
        if not deadline:
            return None
        try:
            if now < datetime.fromisoformat(deadline):
                return None
        except ValueError:
            return None
        target = self._get_admin_moments_target_chat(None)
        return self.cancel_admin_forward_draft(target, message="这次转发任务已超时取消")

    def handle_select_api_config(self, chat, message):
        """处理 /选择接口 N 指令：切换到第 N 个接口配置（1-indexed）"""
        num_str = re.sub("/选择接口", "", message.content).strip()
        try:
            n = int(num_str)
        except ValueError:
            return chat.SendMsg("接口序号无效，请输入数字，如：/选择接口 2")
        idx = n - 1
        if idx < 0 or idx >= len(self.config.api_configs):
            return chat.SendMsg(f"接口 {n} 不存在，当前共 {len(self.config.api_configs)} 个接口")
        self.config.config['api_index'] = idx
        self.config.save_config()
        self.config.refresh_config()
        self.api = self._init_api()
        self.api_cache = {}   # 默认接口已切换，清除群组接口缓存
        self._reset_chat_api_failover_state(active_index=idx)
        cfg = self.config.api_configs[idx]
        return chat.SendMsg(f"已切换至接口 {n}\nSDK：{cfg.get('sdk', '')}\n模型：{cfg.get('model', '')}")

    def handle_clear_memory(self, chat, message):
        """处理 /清除记忆 指令：清除管理员（当前聊天）的对话记忆"""
        if not self.memory_manager:
            return chat.SendMsg("记忆功能未初始化")
        self.memory_manager.clear_messages(self.config.cmd)
        return chat.SendMsg(f"已清除「{self.config.cmd}」的对话记忆")

    def handle_clear_user_memory(self, chat, message):
        """处理 /清除用户记忆 xxx 指令：清除指定用户/群的记忆"""
        name = re.sub("/清除用户记忆", "", message.content).strip()
        if not name:
            return chat.SendMsg("请提供用户或群名称，如：/清除用户记忆 张三")
        if not self.memory_manager:
            return chat.SendMsg("记忆功能未初始化")
        self.memory_manager.clear_messages(name)
        return chat.SendMsg(f"已清除「{name}」的对话记忆")

    def handle_clear_all_memory(self, chat, message):
        """处理 /清除全部记忆 指令：清除所有对话记忆"""
        if not self.memory_manager:
            return chat.SendMsg("记忆功能未初始化")
        count = self.memory_manager.clear_all_messages()
        return chat.SendMsg(f"已清除所有对话记忆（共 {count} 个会话）")

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

    def add_chat_to_listen(self, chat):
        return listening.add_chat_to_listen(self, chat)

    def is_chat_listened(self, chat):
        return listening.is_chat_listened(self, chat)

    def ALLListen_mode(self, last_time, timeout=10):
        if not bool(getattr(self.wx, "is_ui_owner_facade", False)):
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
            "text_reply_limit_switch": getattr(self.config, "text_reply_limit_switch", False),
            "text_reply_limit_count": getattr(self.config, "text_reply_limit_count", 99),
            "text_reply_limit_hours": getattr(self.config, "text_reply_limit_hours", 24),
            "pause_chat_reply":      self._pause_chat_reply or getattr(self.config, "chat_listen_only", False),
            "pause_group_reply":     self._pause_group_reply or getattr(self.config, "group_listen_only", False),
            **listening.listener_recovery_snapshot(self),
        }

    def stop_wxbot(self):
        """Request a stop without waiting for a potentially stuck UI call."""
        try:
            self._ensure_stop_requested_event().set()
            self.run_flag = False
            self._cancel_pending_private_message_timers()
            self._clear_chat_memory_background_state()
            owner = getattr(self, "_ui_owner", None)
            if owner is not None:
                owner.cancel_pending()
            log(level="WARNING", message='siver_wxbot正在停止！！')
            return True
        except Exception as e:
            nickname = getattr(getattr(self, "wx", None), "nickname", "wxbot")
            self.is_err(nickname + ' wxbot机器人关闭程序执行出错！！', e)
            return False

    def _finish_wxbot_stop(self):
        """Run the real listener shutdown on the robot thread after its loop exits."""
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
            self._finalize_private_outbound_echo_fallbacks_on_stop()
            log(level="WARNING", message='siver_wxbot安全退出！！')
            return True
        listener = getattr(self, "wx", None)
        if listener and hasattr(listener, "StopListening"):
            listener.StopListening()
        self._finalize_private_outbound_echo_fallbacks_on_stop()
        log(level="WARNING", message='siver_wxbot安全退出！！')
        return True

    def main(self):
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
                try:
                    if self.wx and hasattr(self.wx, "StopListening"):
                        self.wx.StopListening()
                except Exception as stop_exc:
                    log(level="WARNING", message=f"启动中停止监听失败：{stop_exc}")
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
                            # 回调函数已出错，停止所有监听并退出主循环
                            self.wx.StopListening()
                            log(level="ERROR", message="检测到回调函数出错!!已停止所有监听并跳出主线程!!")
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
                if self.config.new_frined_switch:
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
                    self._check_admin_moments_auto_preview()
                except Exception as e:
                    log(level="ERROR", message=f"管理员发圈自动预览出错：{e}")

                try:
                    if self.is_stop_requested():
                        break
                    self._check_admin_forward_timeout()
                except Exception as e:
                    log(level="ERROR", message=f"管理员转发流程超时检查出错：{e}")

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
