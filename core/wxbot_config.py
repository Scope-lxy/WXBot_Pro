"""WXBot configuration loading and normalization helpers."""

from __future__ import annotations

import json
import os
import random
import sys
import time
from datetime import datetime

from core.account_storage import DEFAULT_ACCOUNT_ID, account_module_file, ensure_default_account, resolve_account_id
from core.api import APIConfigSnapshot, build_api_config_snapshot, default_tts_config, normalize_tts_settings
from core.config import coerce_float_range, coerce_int_range
from core.logger import log
from core.prompt_system import CHAT_MEMORY_DEFAULT_PROTECTED_RECENT_COUNT
from feature.ai_material_outreach import normalize_ai_material_outreach_config
from feature.contacts import (
    coerce_auto_maintenance_full_scan_interval_days,
    coerce_auto_maintenance_interval_minutes,
    coerce_auto_maintenance_window_time,
    normalize_auto_maintenance_batch_size,
)
from feature.material_outreach import (
    DEFAULT_AI_PREFACE_GOAL,
    load_json_list,
    normalize_material_outreach_task,
    normalize_material_source_pool_limit_map,
    save_json_list,
)
from feature.material_outreach_preface import normalize_preface_pending_queue
from feature.material_outreach_storage import MaterialOutreachStorage
from feature.moments_tasks import (
    deserialize_moments_task_collection,
    serialize_moments_task_collection,
)
from feature.scheduled_message_tasks import (
    deserialize_scheduled_message_task_collection,
    normalize_scheduled_message_task_payload,
    serialize_scheduled_message_task_collection,
)
from feature.task_workbench_storage import TaskWorkbenchStorage
from feature.voice_reply import DEFAULT_CHAT_VOICE_REPLY_KEYWORDS, DEFAULT_GROUP_VOICE_REPLY_KEYWORDS
LONG_REPLY_SEGMENT_CHARS = 1000
DEFAULT_VOICE_TRANSCRIPTION_FALLBACK_TEXT = "刚才那条语音，我有点没听清"


class WXBotConfig:
    """
    微信机器人配置类
    负责从 config.json 中加载、保存、刷新配置，
    以及对监听用户列表、群组列表等进行增删管理。
    """

    def __init__(self):
        _base = os.path.dirname(sys.executable) if hasattr(sys, '_MEIPASS') else os.path.abspath(".")
        self.DATA_DIR = os.path.join(_base, 'data')
        self.CONFIG_DIR = os.path.join(self.DATA_DIR, 'config')
        self.CONFIG_FILE = os.path.join(self.CONFIG_DIR, 'config.json')
        self.prompt_dir  = os.path.join(self.DATA_DIR, 'prompt')
        os.makedirs(self.CONFIG_DIR, exist_ok=True)
        self.config = {}

        # ---------- 全局监听开关 ----------
        self.AllListen_switch = False   # True=黑名单模式，False=白名单模式
        self.chat_listen_only = False    # 私聊只监听不 AI 回复

        # ---------- 用户与权限 ----------
        self.listen_list = []           # 白名单模式监听用户列表
        self.global_blacklist = []      # 全局监听模式黑名单用户列表
        self.cmd = ""                   # 管理员账号（命令接收者）

        # ---------- AI 接口配置 ----------
        self.api_configs = []           # 接口配置列表，每项含 sdk/key/url/model
        self.api_index = 0              # 当前使用的接口索引
        self.backup_chat_api_index = -1
        self.backup_chat_api_failover_threshold = 3
        self.current_api_config = APIConfigSnapshot()
        self.prompt   = ""
        self.AtMe     = ""             # 机器人被 @ 的标识（如 "@机器人昵称"）

        # ---------- 群聊配置 ----------
        self.group = []                 # 监听的群聊列表
        self.group_api_map = {}         # 群聊专属接口映射 {群名: api_index}
        self.group_switch = False       # 群机器人总开关
        self.group_listen_only = False   # 群聊只监听不 AI 回复
        self.group_reply_at = False     # 群聊是否仅在被 @ 时才回复
        self.group_welcome = False      # 群新人欢迎语开关
        self.group_welcome_random = 1.0 # 群新人欢迎语触发概率（0.0~1.0）
        self.group_welcome_msg = "欢迎新朋友！请先查看群公告！本消息由wxautox发送!"

        # ---------- 新好友配置 ----------
        self.new_frined_switch = False        # 自动通过新好友开关
        self.new_friend_archive_switch = True # 通过好友后自动修改备注和标签
        self.new_frien_reply_switch = False   # 新好友自动回复开关
        self.new_frien_msg = []               # 通过后自动发送的打招呼消息列表
        self.new_friend_remark_use_nickname = True
        self.new_friend_remark_prefix_timestamp = False
        self.new_friend_remark_suffix_timestamp = False

        # ---------- 关键词回复配置 ----------
        self.chat_keyword_switch = False    # 私聊关键词回复开关
        self.group_keyword_switch = False   # 群聊关键词回复开关
        self.group_keyword_at_only = False  # 群聊关键词仅被@时触发
        self.keyword_dict = {}              # 关键词 -> 回复内容 字典

        # ---------- 自定义转发配置 ----------
        self.custom_forward_switch = False  # 自定义转发总开关
        self.custom_forward_list   = []     # 自定义转发规则列表

        # ---------- 多 Prompt 配置 ----------
        self.default_prompt   = "默认"      # 全局/fallback prompt 文件名（不含 .md）
        self.chat_prompt_map  = {}          # 白名单模式私聊用户 -> prompt 名称
        self.chat_api_map     = {}          # 私聊白名单用户 -> API 接口索引
        self.chat_tts_map     = {}          # 私聊白名单用户 -> TTS 接口索引
        self.group_prompt_map = {}          # 群组名称 -> prompt 名称

        # ---------- 会话记忆配置 ----------
        self.chat_memory_switch = True
        self.chat_memory_exclude_list = []
        self.chat_memory_message_threshold = 100
        self.chat_memory_interval_hours = 12
        self.chat_memory_protected_recent_count = CHAT_MEMORY_DEFAULT_PROTECTED_RECENT_COUNT

        # ---------- 定时消息配置 ----------
        self.scheduled_message_task_list = []  # 统一定时消息任务列表

        # ---------- 素材转发配置 ----------
        self.contact_directory_auto_maintenance_switch = False
        self.contact_directory_auto_maintenance_batch_size = 10
        self.contact_directory_auto_maintenance_interval_minutes = 10
        self.contact_directory_auto_maintenance_full_scan_interval_days = 7
        self.contact_directory_auto_maintenance_window_start = "00:00"
        self.contact_directory_auto_maintenance_window_end = "23:59"
        self.material_source_list = []
        self.material_source_silent = True
        self.material_source_pool_limit_map = {}
        self.material_outreach_list = []
        self.ai_material_outreach_switch = False
        self.ai_material_outreach_sensitivity = "conservative"
        self.ai_material_outreach_daily_limit_per_friend = 3
        self.ai_material_outreach_delay_min_seconds = 10
        self.ai_material_outreach_delay_max_seconds = 30
        self.ai_material_outreach_preface_enabled = True
        self.ai_material_outreach_preface_goal = DEFAULT_AI_PREFACE_GOAL
        self.ai_material_outreach_preface_intensity = ""

        # ---------- 朋友圈配置 ----------
        self.moments_api_index = 0                 # 发朋友圈专用接口
        self.moments_task_list = []             # 统一发朋友圈任务列表

        # ---------- 随机朋友圈点赞配置 ----------
        self.moments_like_switch = False  # 随机点赞总开关
        self.moments_like_min    = 60     # 随机间隔最小分钟数
        self.moments_like_max    = 120    # 随机间隔最大分钟数

        # ---------- 对话记忆配置 ----------
        self.memory_switch        = True      # 记忆开关（默认开启）
        self.memory_context_switch = True     # 是否把最近聊天记录带入 AI 上下文
        self.memory_max_count     = 5000     # 单窗口最多存储条数（上限 5000）
        self.memory_context_count = 50       # AI 请求时带入条数
        self.memory_context_assistant_count = 10  # AI 请求时保留的机器人历史回复条数
        self.memory_context_repair_low_risk_switch = True
        self.memory_context_repair_high_risk_switch = False

        # ---------- 发送延迟配置 ----------
        self.reply_delay_switch = True   # 模拟人工操作延迟开关（默认开启）
        self.reply_delay_first_min = 1   # 首条回复最小延迟秒数
        self.reply_delay_first_max = 5   # 首条回复最大延迟秒数
        self.reply_delay_split_speed_mode = "fast"  # 拆分消息发送延迟档位
        self.reply_delay_split_min = 1   # 拆分消息最小延迟秒数
        self.reply_delay_split_max = 2   # 拆分消息最大延迟秒数
        self.wxauto_save_cache_retention_days = 30  # wxauto_save 缓存自动清理周期，0=不清理
        self.clean_ai_reply_switch = True  # AI 回复清洗开关
        self.current_account_wx_id = ""

        # 初始化时自动加载配置并同步到属性
        self.load_config()
        self.update_global_config()

    # ----------------------------------------------------------
    # 配置文件读写
    # ----------------------------------------------------------

    def load_config(self):
        """从 config.json 加载配置到 self.config 字典"""
        # 若配置文件不存在，先创建默认配置
        if not os.path.exists(self.CONFIG_FILE):
            self.create_new_config_file()
        try:
            self.current_account_wx_id = resolve_account_id(
                getattr(self, "current_account_wx_id", ""),
                fallback_default=True,
            )
            if self.current_account_wx_id == DEFAULT_ACCOUNT_ID:
                ensure_default_account(self.DATA_DIR)
            with open(self.CONFIG_FILE, 'r', encoding='utf-8-sig') as file:
                self.config = json.load(file)
                self._normalize_scheduled_config_lists()
                self._load_account_scoped_keyword_rules()
                self._load_account_scoped_custom_forward_rules()
                self._load_account_scoped_scheduled_message_tasks()
                self._load_account_scoped_material_outreach_tasks()
                self._load_account_scoped_moments_tasks()
                log(message="配置文件加载成功")
        except Exception as e:
            log(level="ERROR", message="打开配置文件失败，请检查配置文件！" + str(e))
            # 配置文件损坏或缺失时阻塞程序，避免带着错误配置继续运行
            while True:
                time.sleep(100)

    def _normalize_scheduled_config_lists(self):
        if not isinstance(self.config, dict):
            self.config = {}
        self.config['scheduled_message_task_list'] = [
            normalize_scheduled_message_task_payload(task)
            for task in self.config.get('scheduled_message_task_list', [])
            if isinstance(task, dict)
        ]
        self._normalize_material_task_list(self.config)

    def _normalize_material_task_list(self, config):
        if not isinstance(config, dict):
            return
        normalized_material_tasks = []
        for task in config.get('material_outreach_list', []):
            if not isinstance(task, dict):
                continue
            normalized_material_tasks.append(normalize_material_outreach_task(task))
        config['material_outreach_list'] = normalized_material_tasks

    def create_new_config_file(self):
        """若配置文件不存在，则创建一份包含默认值的配置文件"""
        try:
            if not os.path.exists(self.CONFIG_FILE):
                base_config = {
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
                    "chat_memory_protected_recent_count": CHAT_MEMORY_DEFAULT_PROTECTED_RECENT_COUNT,
                    "scheduled_message_task_list": [],
                    "contact_directory_auto_maintenance_switch": False,
                    "contact_directory_auto_maintenance_batch_size": 10,
                    "contact_directory_auto_maintenance_interval_minutes": 10,
                    "contact_directory_auto_maintenance_full_scan_interval_days": 7,
                    "contact_directory_auto_maintenance_window_start": "00:00",
                    "contact_directory_auto_maintenance_window_end": "23:59",
                    "material_source_list": [],
                    "material_source_silent": True,
                    "material_source_pool_limit_map": {},
                    "material_outreach_list": [],
                    "moments_api_index": 0,
                    "moments_task_list": [],
                    "moments_like_switch": False,
                    "moments_like_min": 60,
                    "moments_like_max": 120,
                    "everyday_start_stop_bot_switch": False,
                    "everyday_start_bot_time": "08:00",
                    "everyday_stop_bot_time": "23:00",
                    "memory_switch": True,
                    "memory_context_switch": True,
                    "memory_max_count": 5000,
                    "memory_context_count": 50,
                    "memory_context_assistant_count": 10,
                    "memory_context_repair_low_risk_switch": True,
                    "memory_context_repair_high_risk_switch": False,
                    "reply_delay_switch": True,
                    "reply_delay_first_min": 1,
                    "reply_delay_first_max": 5,
                    "reply_delay_split_speed_mode": "fast",
                    "reply_delay_split_min": 1,
                    "reply_delay_split_max": 2,
                    "wxauto_save_cache_retention_days": 30,
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
                    "tts_configs": [default_tts_config()],
                    "tts_index": 0,
                    "chat_voice_reply_switch": False,
                    "chat_voice_reply_trigger_modes": ["keyword"],
                    "chat_voice_reply_request_keywords": list(DEFAULT_CHAT_VOICE_REPLY_KEYWORDS),
                    "chat_voice_reply_cooldown_minutes": 10,
                    "chat_voice_reply_limit_count": 50,
                    "chat_voice_reply_limit_hours": 24,
                    "chat_voice_session_minutes": 10,
                    "chat_voice_session_turns": 5,
                    "group_voice_reply_switch": False,
                    "group_voice_reply_request_keywords": list(DEFAULT_GROUP_VOICE_REPLY_KEYWORDS),
                    "group_voice_reply_cooldown_minutes": 0,
                    "group_voice_reply_limit_count": 99,
                    "group_voice_reply_limit_hours": 24,
                    "siver_panel_enabled": False,
                    "siver_panel_activation_code": "",
                    "siver_panel_slug": "",
                    "siver_panel_install_id": "",
                    "siver_panel_machine_fingerprint": "",
                    "siver_panel_device_id": "",
                    "siver_panel_device_secret": "",
                    "siver_panel_base_url": "https://panel.siver.top",
                    "siver_panel_ws_url": "wss://panel.siver.top/relay/ws",
                    "siver_panel_panel_url": "",
                    "siver_panel_service_expire_at": "",
                    "siver_panel_last_error_code": "",
                    "siver_panel_last_error_message": "",
                }
                with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(base_config, f, ensure_ascii=False, indent=4)
                os.makedirs(self.prompt_dir, exist_ok=True)
                default_prompt_path = os.path.join(self.prompt_dir, "默认.md")
                if not os.path.exists(default_prompt_path):
                    with open(default_prompt_path, "w", encoding="utf-8") as f:
                        f.write("你是一个ai回复助手，请根据用户的问题给出回答,回复尽量保持在30字以内")
                log(message=f"已创建默认配置文件：\n{os.path.abspath(self.CONFIG_FILE)}\n请根据需求修改配置后重启")
        except Exception as e:
            log(level="ERROR", message="创建默认配置文件失败，请检查配置文件！" + str(e))
            while True:
                time.sleep(100)

    def save_config(self):
        """将当前 self.config 字典持久化写回 config.json"""
        try:
            self._normalize_scheduled_config_lists()
            self._save_account_scoped_keyword_rules()
            self._save_account_scoped_custom_forward_rules()
            self._save_account_scoped_scheduled_message_tasks()
            self._save_account_scoped_material_outreach_tasks()
            self._save_account_scoped_moments_tasks()
            persisted = dict(self.config)
            persisted.pop('keyword_dict', None)
            persisted.pop('custom_forward_list', None)
            persisted.pop('scheduled_message_task_list', None)
            persisted.pop('material_outreach_list', None)
            persisted.pop('moments_task_list', None)
            persisted.pop('prompt', None)
            with open(self.CONFIG_FILE, 'w', encoding='utf-8') as file:
                json.dump(persisted, file, ensure_ascii=False, indent=4)
        except Exception as e:
            log(level="ERROR", message="保存配置文件失败:" + str(e))

    def refresh_config(self):
        """重新加载配置文件，并将最新值同步到所有属性"""
        self.load_config()
        self.update_global_config()

    def bind_account_wx_id(self, wx_id):
        self.current_account_wx_id = resolve_account_id(wx_id, fallback_default=True)
        if self.current_account_wx_id == DEFAULT_ACCOUNT_ID:
            ensure_default_account(self.DATA_DIR)
        self._load_account_scoped_keyword_rules()
        self._load_account_scoped_custom_forward_rules()
        self._load_account_scoped_scheduled_message_tasks()
        self._load_account_scoped_material_outreach_tasks()
        self._load_account_scoped_moments_tasks()
        self.update_global_config()

    def _keyword_rules_file(self, *, wx_id=None, create_parent=False):
        account_wx_id = resolve_account_id(wx_id or self.current_account_wx_id, fallback_default=True)
        if not account_wx_id:
            return None
        return account_module_file(
            self.DATA_DIR,
            account_wx_id,
            "keyword_reply",
            "rules.json",
            create_parent=create_parent,
        )

    def _load_account_scoped_keyword_rules(self):
        rules = {}
        rules_file = self._keyword_rules_file()
        if rules_file and os.path.exists(rules_file):
            try:
                with open(rules_file, 'r', encoding='utf-8-sig') as file:
                    raw = json.load(file)
                rules = raw if isinstance(raw, dict) else {}
            except Exception:
                rules = {}
        if not isinstance(self.config, dict):
            self.config = {}
        self.config["keyword_dict"] = rules
        return self.config["keyword_dict"]

    def _save_account_scoped_keyword_rules(self):
        rules_file = self._keyword_rules_file(create_parent=True)
        if not rules_file:
            return
        rules = self.config.get("keyword_dict", {})
        if not isinstance(rules, dict):
            rules = {}
        with open(rules_file, 'w', encoding='utf-8') as file:
            json.dump(rules, file, ensure_ascii=False, indent=4)

    def _custom_forward_rules_file(self, *, wx_id=None, create_parent=False):
        account_wx_id = resolve_account_id(wx_id or self.current_account_wx_id, fallback_default=True)
        if not account_wx_id:
            return None
        return account_module_file(
            self.DATA_DIR,
            account_wx_id,
            "custom_forward",
            "rules.json",
            create_parent=create_parent,
        )

    def _load_account_scoped_custom_forward_rules(self):
        rules = []
        rules_file = self._custom_forward_rules_file()
        if rules_file:
            rules = load_json_list(rules_file)
        if not isinstance(self.config, dict):
            self.config = {}
        self.config["custom_forward_list"] = [rule for rule in rules if isinstance(rule, dict)]
        return self.config["custom_forward_list"]

    def _save_account_scoped_custom_forward_rules(self):
        rules_file = self._custom_forward_rules_file(create_parent=True)
        if not rules_file:
            return
        save_json_list(
            rules_file,
            [rule for rule in self.config.get("custom_forward_list", []) if isinstance(rule, dict)],
        )

    def _scheduled_message_tasks_file(self, *, wx_id=None, create_parent=False):
        account_wx_id = resolve_account_id(wx_id or self.current_account_wx_id, fallback_default=True)
        if not account_wx_id:
            return None
        return account_module_file(
            self.DATA_DIR,
            account_wx_id,
            "scheduled_message",
            "tasks.json",
            create_parent=create_parent,
        )

    def _scheduled_message_runtime_file(self, *, wx_id=None, create_parent=False):
        account_wx_id = resolve_account_id(wx_id or self.current_account_wx_id, fallback_default=True)
        if not account_wx_id:
            return None
        return account_module_file(
            self.DATA_DIR,
            account_wx_id,
            "scheduled_message",
            "runtime.json",
            create_parent=create_parent,
        )

    def _scheduled_message_history_file(self, *, wx_id=None, create_parent=False):
        account_wx_id = resolve_account_id(wx_id or self.current_account_wx_id, fallback_default=True)
        if not account_wx_id:
            return None
        return account_module_file(
            self.DATA_DIR,
            account_wx_id,
            "scheduled_message",
            "history.json",
            create_parent=create_parent,
        )

    def _task_storage_data_dir(self):
        return str(getattr(self, "DATA_DIR", "") or "").strip()

    def _task_storage_wx_id(self, wx_id=None):
        return resolve_account_id(
            wx_id or getattr(self, "current_account_wx_id", ""),
            fallback_default=True,
        )

    def _scheduled_message_storage(self, *, wx_id=None):
        data_dir = self._task_storage_data_dir()
        if not data_dir:
            return None
        return TaskWorkbenchStorage(data_dir, self._task_storage_wx_id(wx_id), "scheduled_message")

    def _load_json_object_file(self, path):
        if not path:
            return {}
        try:
            with open(path, "r", encoding="utf-8-sig") as file:
                data = json.load(file)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _save_json_object_file(self, path, payload):
        if not path:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(payload if isinstance(payload, dict) else {}, file, ensure_ascii=False, indent=4)

    def _load_account_scoped_scheduled_message_tasks(self):
        definitions = []
        tasks_file = self._scheduled_message_tasks_file()
        if tasks_file:
            definitions = load_json_list(tasks_file)
        runtime_map = self._load_json_object_file(self._scheduled_message_runtime_file())
        history_map = self._load_json_object_file(self._scheduled_message_history_file())
        if not isinstance(self.config, dict):
            self.config = {}
        self.config["scheduled_message_task_list"] = deserialize_scheduled_message_task_collection(
            definitions,
            runtime_map,
            history_map,
        )
        return self.config["scheduled_message_task_list"]

    def _save_account_scoped_scheduled_message_tasks(self):
        storage = self._scheduled_message_storage()
        if storage is None:
            return
        normalized = [
            normalize_scheduled_message_task_payload(task)
            for task in self.config.get("scheduled_message_task_list", [])
            if isinstance(task, dict)
        ]
        definitions, _runtime_map, _history_map = serialize_scheduled_message_task_collection(normalized)
        storage.save_tasks(definitions)

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
            self.DATA_DIR,
            resolve_account_id(
                wx_id or getattr(self, "current_account_wx_id", ""),
                fallback_default=True,
            ),
        )

    def _load_account_scoped_material_outreach_tasks(self):
        tasks = []
        tasks_file = self._material_outreach_tasks_file()
        if tasks_file:
            tasks = load_json_list(tasks_file)
        if not isinstance(self.config, dict):
            self.config = {}
        material_config = {'material_outreach_list': tasks}
        self._normalize_material_task_list(material_config)
        self.config["material_outreach_list"] = [
            task for task in material_config.get("material_outreach_list", []) if isinstance(task, dict)
        ]
        return self.config["material_outreach_list"]

    def _save_account_scoped_material_outreach_tasks(self):
        tasks_file = self._material_outreach_tasks_file(create_parent=True)
        if not tasks_file:
            return
        material_config = {
            "material_outreach_list": [
                task for task in self.config.get("material_outreach_list", []) if isinstance(task, dict)
            ]
        }
        self._normalize_material_task_list(material_config)
        save_json_list(tasks_file, material_config.get("material_outreach_list", []))

    def _load_material_outreach_runtime(self):
        return self._material_outreach_store().load_runtime()

    def _save_material_outreach_runtime(self, payload):
        return self._material_outreach_store().save_runtime(payload)

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
        return self._material_outreach_store().append_send_record(record, limit=limit)

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

    def _update_material_progress_records_for_send(self, snapshot, targets, *, success, error="", now=None, limit=1000):
        return self._material_outreach_store().update_progress_records_for_send(
            snapshot,
            targets,
            success=success,
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

    def _moments_tasks_file(self, *, wx_id=None, create_parent=False):
        account_wx_id = resolve_account_id(wx_id or self.current_account_wx_id, fallback_default=True)
        if not account_wx_id:
            return None
        return account_module_file(
            self.DATA_DIR,
            account_wx_id,
            "moments",
            "tasks.json",
            create_parent=create_parent,
        )

    def _moments_runtime_file(self, *, wx_id=None, create_parent=False):
        account_wx_id = resolve_account_id(wx_id or self.current_account_wx_id, fallback_default=True)
        if not account_wx_id:
            return None
        return account_module_file(
            self.DATA_DIR,
            account_wx_id,
            "moments",
            "runtime.json",
            create_parent=create_parent,
        )

    def _moments_history_file(self, *, wx_id=None, create_parent=False):
        account_wx_id = resolve_account_id(wx_id or self.current_account_wx_id, fallback_default=True)
        if not account_wx_id:
            return None
        return account_module_file(
            self.DATA_DIR,
            account_wx_id,
            "moments",
            "history.json",
            create_parent=create_parent,
        )

    def _moments_task_storage(self, *, wx_id=None):
        data_dir = self._task_storage_data_dir()
        if not data_dir:
            return None
        return TaskWorkbenchStorage(data_dir, self._task_storage_wx_id(wx_id), "moments")

    def _load_account_scoped_moments_tasks(self):
        definitions = []
        tasks_file = self._moments_tasks_file()
        if tasks_file:
            definitions = load_json_list(tasks_file)
        runtime_map = self._load_json_object_file(self._moments_runtime_file())
        history_map = self._load_json_object_file(self._moments_history_file())
        if not isinstance(self.config, dict):
            self.config = {}
        self.config["moments_task_list"] = deserialize_moments_task_collection(
            definitions,
            runtime_map,
            history_map,
        )
        return self.config["moments_task_list"]

    def _save_account_scoped_moments_tasks(self):
        storage = self._moments_task_storage()
        if storage is None:
            return
        normalized = [task for task in self.config.get("moments_task_list", []) if isinstance(task, dict)]
        definitions, _runtime_map, _history_map = serialize_moments_task_collection(normalized)
        storage.save_tasks(definitions)

    def init_prompt_dir(self):
        """确保 prompt 目录存在；空目录时写入默认 prompt"""
        os.makedirs(self.prompt_dir, exist_ok=True)
        try:
            md_files = [f for f in os.listdir(self.prompt_dir) if f.endswith('.md')]
        except Exception:
            md_files = []
        if not md_files:
            try:
                with open(os.path.join(self.prompt_dir, '默认.md'), 'w', encoding='utf-8') as f:
                    f.write("你是一个ai回复助手，请根据用户的问题给出回答,回复尽量保持在30字以内")
            except Exception as e:
                log(level="ERROR", message=f"创建默认 prompt 文件失败: {e}")

    def get_prompt_content(self, name):
        """按名称读取 prompt 文件内容，找不到时 fallback 到 default_prompt，最终返回空字符串"""
        if not name:
            name = self.default_prompt
        path = os.path.join(self.prompt_dir, f'{name}.md')
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception:
                pass
        # fallback 到 default_prompt
        if name != self.default_prompt:
            fallback = os.path.join(self.prompt_dir, f'{self.default_prompt}.md')
            if os.path.exists(fallback):
                try:
                    with open(fallback, 'r', encoding='utf-8') as f:
                        return f.read()
                except Exception:
                    pass
        return ""

    # ----------------------------------------------------------
    # 配置同步：将 config 字典中的值同步到实例属性
    # ----------------------------------------------------------

    def update_global_config(self):
        """将 self.config 字典中的各配置项同步到对应实例属性"""
        self.api_configs = self.config.get('api_configs', [
            {"sdk": "DusAPI", "key": "", "url": "https://api.dusapi.com", "model": "gpt-5"},
            {"sdk": "DusAPI", "key": "", "url": "https://api.dusapi.com", "model": "claude-sonnet-4-6"},
        ])
        self.api_index = self.config.get('api_index', 0)
        if self.api_index >= len(self.api_configs):
            self.api_index = 0
        try:
            self.backup_chat_api_index = int(self.config.get('backup_chat_api_index', -1))
        except (TypeError, ValueError):
            self.backup_chat_api_index = -1
        if (
            len(self.api_configs) < 2
            or self.backup_chat_api_index < 0
            or self.backup_chat_api_index >= len(self.api_configs)
            or self.backup_chat_api_index == self.api_index
        ):
            self.backup_chat_api_index = -1
        self.backup_chat_api_failover_threshold = self._coerce_int_range(
            self.config.get('backup_chat_api_failover_threshold', 3), 3, 1, 10
        )
        self.api_capability_map = self.config.get('api_capability_map', {})
        if not isinstance(self.api_capability_map, dict):
            self.api_capability_map = {}
            self.config['api_capability_map'] = {}

        # 当前默认聊天接口快照
        _cur = self.api_configs[self.api_index] if self.api_configs else {}
        self.prompt   = ""
        self.current_api_config = build_api_config_snapshot(
            _cur,
            prompt=self.prompt,
            max_retries=getattr(self, "max_retries", 5),
            interface_index=self.api_index,
        )

        # 微信基础配置
        self.cmd            = self.config.get('admin', "")
        self.config.setdefault('listen_list', [])
        self.config.setdefault('global_blacklist', [])
        self.listen_list          = self.config.get('listen_list', [])
        self.global_blacklist     = self.config.get('global_blacklist', [])
        if not isinstance(self.listen_list, list):
            self.listen_list = []
            self.config['listen_list'] = []
        if not isinstance(self.global_blacklist, list):
            self.global_blacklist = []
            self.config['global_blacklist'] = []
        self.AllListen_switch     = self.config.get('AllListen_switch')
        self.AllListen_filter_mute = bool(self.config.get('AllListen_filter_mute', True))
        self.chat_listen_only     = bool(self.config.get('chat_listen_only', False))

        # 群聊配置
        self.group                = self.config.get('group', [])
        self.group_api_map        = self.config.get('group_api_map', {})
        self.group_switch         = self.config.get('group_switch')
        self.group_listen_only    = bool(self.config.get('group_listen_only', False))
        self.group_reply_at       = self.config.get('group_reply_at')
        self.group_reply_at_msg   = bool(self.config.get('group_reply_at_msg', True))
        self.group_reply_quote    = bool(self.config.get('group_reply_quote', False))
        self.group_welcome        = self.config.get('group_welcome')
        self.group_welcome_random = self.config.get('group_welcome_random')
        self.group_welcome_msg    = self.config.get('group_welcome_msg', '')

        # 新好友配置
        self.new_frined_switch       = self.config.get('new_friend_switch')
        self.new_frien_msg           = self.config.get('new_friend_msg', {"text": "", "files": []})
        self.new_friend_archive_switch = bool(self.config.get('new_friend_archive_switch', True))
        self.new_frien_reply_switch  = bool(
            str((self.new_frien_msg or {}).get('text', '')).strip()
            or ((self.new_frien_msg or {}).get('files') or [])
        )
        self.new_friend_check_min    = max(60, int(self.config.get('new_friend_check_min', 60)))
        self.new_friend_check_max    = min(3600, max(self.new_friend_check_min, int(self.config.get('new_friend_check_max', 300))))
        self.new_friend_remark_use_nickname = bool(self.config.get('new_friend_remark_use_nickname', True))
        self.new_friend_remark_prefix = self.config.get('new_friend_remark_prefix', '')
        self.new_friend_remark_prefix_timestamp = bool(self.config.get('new_friend_remark_prefix_timestamp', False))
        self.new_friend_remark_suffix = self.config.get('new_friend_remark_suffix', '_机器人备注')
        self.new_friend_remark_suffix_timestamp = bool(self.config.get('new_friend_remark_suffix_timestamp', False))
        self.new_friend_tags         = self.config.get('new_friend_tags', [])

        # 关键词配置
        self.chat_keyword_switch   = bool(self.config.get('chat_keyword_switch', False))
        self.group_keyword_switch  = bool(self.config.get('group_keyword_switch', False))
        self.group_keyword_at_only = bool(self.config.get('group_keyword_at_only', False))
        self.keyword_dict          = self.config.get('keyword_dict', {})

        # 定时消息配置
        self.scheduled_message_task_list = self.config.get('scheduled_message_task_list', [])
        if not isinstance(self.scheduled_message_task_list, list):
            self.scheduled_message_task_list = []
        self.config['scheduled_message_task_list'] = self.scheduled_message_task_list

        # 素材转发配置
        self.contact_directory_auto_maintenance_switch = bool(
            self.config.get('contact_directory_auto_maintenance_switch', False)
        )
        self.contact_directory_auto_maintenance_batch_size = normalize_auto_maintenance_batch_size(
            self.config.get('contact_directory_auto_maintenance_batch_size', 10)
        )
        self.contact_directory_auto_maintenance_interval_minutes = coerce_auto_maintenance_interval_minutes(
            self.config.get('contact_directory_auto_maintenance_interval_minutes', 10)
        )
        self.contact_directory_auto_maintenance_full_scan_interval_days = coerce_auto_maintenance_full_scan_interval_days(
            self.config.get('contact_directory_auto_maintenance_full_scan_interval_days', 7)
        )
        self.contact_directory_auto_maintenance_window_start = coerce_auto_maintenance_window_time(
            self.config.get('contact_directory_auto_maintenance_window_start', '00:00'),
            '00:00',
        )
        self.contact_directory_auto_maintenance_window_end = coerce_auto_maintenance_window_time(
            self.config.get('contact_directory_auto_maintenance_window_end', '23:59'),
            '23:59',
        )
        self.config['contact_directory_auto_maintenance_batch_size'] = self.contact_directory_auto_maintenance_batch_size
        self.config['contact_directory_auto_maintenance_interval_minutes'] = self.contact_directory_auto_maintenance_interval_minutes
        self.config['contact_directory_auto_maintenance_full_scan_interval_days'] = self.contact_directory_auto_maintenance_full_scan_interval_days
        self.config['contact_directory_auto_maintenance_window_start'] = self.contact_directory_auto_maintenance_window_start
        self.config['contact_directory_auto_maintenance_window_end'] = self.contact_directory_auto_maintenance_window_end
        self.material_source_list = self.config.get('material_source_list', [])
        if not isinstance(self.material_source_list, list):
            self.material_source_list = []
            self.config['material_source_list'] = []
        self.material_source_silent = bool(self.config.get('material_source_silent', True))
        self.material_source_pool_limit_map = normalize_material_source_pool_limit_map(
            self.config.get('material_source_pool_limit_map', {})
        )
        self.config['material_source_pool_limit_map'] = self.material_source_pool_limit_map
        self.material_outreach_list = self.config.get('material_outreach_list', [])
        if not isinstance(self.material_outreach_list, list):
            self.material_outreach_list = []
            self.config['material_outreach_list'] = []
        ai_outreach_config = normalize_ai_material_outreach_config(self.config)
        self.ai_material_outreach_switch = ai_outreach_config["ai_material_outreach_switch"]
        self.ai_material_outreach_sensitivity = ai_outreach_config["ai_material_outreach_sensitivity"]
        self.ai_material_outreach_daily_limit_per_friend = ai_outreach_config["ai_material_outreach_daily_limit_per_friend"]
        self.ai_material_outreach_delay_min_seconds = ai_outreach_config["ai_material_outreach_delay_min_seconds"]
        self.ai_material_outreach_delay_max_seconds = ai_outreach_config["ai_material_outreach_delay_max_seconds"]
        self.ai_material_outreach_preface_enabled = ai_outreach_config["ai_material_outreach_preface_enabled"]
        self.ai_material_outreach_preface_goal = ai_outreach_config["ai_material_outreach_preface_goal"]
        self.ai_material_outreach_preface_intensity = ai_outreach_config["ai_material_outreach_preface_intensity"]

        # 朋友圈配置
        try:
            self.moments_api_index = int(self.config.get('moments_api_index', 0))
        except (TypeError, ValueError):
            self.moments_api_index = 0
        if self.api_configs:
            self.moments_api_index = max(0, min(len(self.api_configs) - 1, self.moments_api_index))
        else:
            self.moments_api_index = 0
        self.config['moments_api_index'] = self.moments_api_index
        self.moments_task_list = self.config.get('moments_task_list', [])
        if not isinstance(self.moments_task_list, list):
            self.moments_task_list = []
            self.config['moments_task_list'] = []

        # 随机朋友圈点赞配置
        self.moments_like_switch = self.config.get('moments_like_switch', False)
        self.moments_like_min    = max(1,    int(self.config.get('moments_like_min', 60)))
        self.moments_like_max    = max(self.moments_like_min, int(self.config.get('moments_like_max', 120)))

        # 对话记忆配置
        self.memory_switch        = self.config.get('memory_switch', True)
        self.memory_context_switch = bool(self.config.get('memory_context_switch', self.memory_switch))
        self.memory_max_count = self._coerce_int_range(
            self.config.get('memory_max_count', 5000), 5000, 100, 5000
        )
        self.memory_context_count = self._coerce_int_range(
            self.config.get('memory_context_count', 50), 50, 1, 100
        )
        if self.memory_context_count > self.memory_max_count:
            self.memory_context_count = self.memory_max_count
        self.memory_context_assistant_count = self._coerce_int_range(
            self.config.get('memory_context_assistant_count', 10), 10, 0, 100
        )
        if self.memory_context_assistant_count > self.memory_context_count:
            self.memory_context_assistant_count = self.memory_context_count
        repair_defaults = {
            "memory_context_repair_low_risk_switch": True,
            "memory_context_repair_high_risk_switch": False,
        }
        repair_needs_save = any(key not in self.config for key in repair_defaults)
        for key, value in repair_defaults.items():
            self.config.setdefault(key, value)
        self.memory_context_repair_low_risk_switch = bool(
            self.config.get("memory_context_repair_low_risk_switch", True)
        )
        self.memory_context_repair_high_risk_switch = bool(
            self.config.get("memory_context_repair_high_risk_switch", False)
        )
        if repair_needs_save:
            self.config.update({
                "memory_context_repair_low_risk_switch": self.memory_context_repair_low_risk_switch,
                "memory_context_repair_high_risk_switch": self.memory_context_repair_high_risk_switch,
            })
            self.save_config()

        # 发送延迟配置
        removed_delay_keys = False
        for key in (f"reply_delay_{suffix}" for suffix in ("min", "max")):
            removed_delay_keys = self.config.pop(key, None) is not None or removed_delay_keys
        _delay_defaults = {
            'reply_delay_switch': True,
            'reply_delay_first_min': 1,
            'reply_delay_first_max': 5,
            'reply_delay_split_speed_mode': 'fast',
            'reply_delay_split_min': 1,
            'reply_delay_split_max': 2,
            'wxauto_save_cache_retention_days': 30,
        }
        _needs_save = removed_delay_keys or any(k not in self.config for k in _delay_defaults)
        for k, v in _delay_defaults.items():
            self.config.setdefault(k, v)
        if _needs_save:
            self.save_config()
            log(message="已自动补充发送延迟配置默认值并写回配置文件")
        self.reply_delay_switch = bool(self.config.get('reply_delay_switch', True))
        self.reply_delay_first_min = self._coerce_int_range(
            self.config.get('reply_delay_first_min', 1), 1, 1, 600
        )
        self.reply_delay_first_max = self._coerce_int_range(
            self.config.get('reply_delay_first_max', 5), 5, 1, 600
        )
        split_speed_mode = str(self.config.get('reply_delay_split_speed_mode', 'fast') or 'fast').strip().lower()
        if split_speed_mode not in ('fast', 'normal', 'slow'):
            split_speed_mode = 'fast'
        self.reply_delay_split_speed_mode = split_speed_mode
        self.config['reply_delay_split_speed_mode'] = split_speed_mode
        self.reply_delay_split_min = self._coerce_int_range(
            self.config.get('reply_delay_split_min', self.reply_delay_first_min),
            self.reply_delay_first_min,
            1,
            600,
        )
        self.reply_delay_split_max = self._coerce_int_range(
            self.config.get('reply_delay_split_max', self.reply_delay_first_max),
            self.reply_delay_first_max,
            1,
            600,
        )
        self.wxauto_save_cache_retention_days = self._coerce_choice_int(
            self.config.get('wxauto_save_cache_retention_days', 30),
            30,
            {0, 7, 30, 90, 180, 360},
        )
        self.config['wxauto_save_cache_retention_days'] = self.wxauto_save_cache_retention_days
        self.clean_ai_reply_switch = bool(self.config.get('clean_ai_reply_switch', True))

        # 图片识别配置
        self.chat_image_recognition_switch  = bool(self.config.get('chat_image_recognition_switch', False))
        self.chat_voice_recognition_switch  = bool(self.config.get('chat_voice_recognition_switch', False))
        voice_fallback_text = str(
            self.config.get('voice_transcription_fallback_text', DEFAULT_VOICE_TRANSCRIPTION_FALLBACK_TEXT) or ''
        ).strip()
        self.voice_transcription_fallback_text = voice_fallback_text
        self.voice_transcription_fallback_reply_once = bool(
            self.config.get('voice_transcription_fallback_reply_once', False)
        )
        self.chat_message_merge_delay = coerce_float_range(
            self.config.get('chat_message_merge_delay', 3.0), 3.0, 0.0, 10.0
        )
        self.chat_image_recognition_api     = int(self.config.get('chat_image_recognition_api', 0))
        self.group_image_recognition_switch = bool(self.config.get('group_image_recognition_switch', False))
        self.group_voice_recognition_switch = bool(self.config.get('group_voice_recognition_switch', False))
        self.group_image_recognition_api    = int(self.config.get('group_image_recognition_api', 0))

        # 自定义转发配置
        self.custom_forward_switch = bool(self.config.get('custom_forward_switch', False))
        self.custom_forward_list   = self.config.get('custom_forward_list', [])

        # 多 Prompt 配置
        self.default_prompt   = self.config.get('default_prompt', '默认')
        self.chat_prompt_map  = self.config.get('chat_prompt_map', {})
        self.chat_api_map     = self.config.get('chat_api_map', {})
        self.chat_tts_map     = self._normalize_chat_tts_map(
            self.config.get('chat_tts_map', {})
        )
        self.group_prompt_map = self.config.get('group_prompt_map', {})
        self.init_prompt_dir()

        # 会话记忆配置
        _chat_memory_defaults = {
            'chat_memory_switch': True,
            'chat_memory_exclude_list': [],
            'chat_memory_message_threshold': 100,
            'chat_memory_interval_hours': 12,
            'chat_memory_protected_recent_count': CHAT_MEMORY_DEFAULT_PROTECTED_RECENT_COUNT,
        }
        _chat_memory_needs_save = any(k not in self.config for k in _chat_memory_defaults)
        for k, v in _chat_memory_defaults.items():
            self.config.setdefault(k, v)
        if _chat_memory_needs_save:
            self.save_config()
            log(message="已自动补充会话记忆配置默认值并写回配置文件")
        self.chat_memory_switch = bool(self.config.get('chat_memory_switch', True))
        self.chat_memory_exclude_list = self.config.get('chat_memory_exclude_list', [])
        if not isinstance(self.chat_memory_exclude_list, list):
            self.chat_memory_exclude_list = []
        self.chat_memory_message_threshold = self._coerce_int_range(
            self.config.get('chat_memory_message_threshold', 100), 100, 10, 200
        )
        self.chat_memory_interval_hours = self._coerce_int_range(
            self.config.get('chat_memory_interval_hours', 12), 12, 1, 72
        )
        self.chat_memory_protected_recent_count = self._coerce_int_range(
            self.config.get('chat_memory_protected_recent_count', CHAT_MEMORY_DEFAULT_PROTECTED_RECENT_COUNT),
            CHAT_MEMORY_DEFAULT_PROTECTED_RECENT_COUNT,
            0,
            200,
        )

        # 接口调用失败时的固定回复
        self.api_error_reply = str(self.config.get('api_error_reply', '') or '').strip()
        self.api_error_reply_once = bool(self.config.get('api_error_reply_once', False))
        self.meta_reply_blocked_reply = str(self.config.get('meta_reply_blocked_reply', '') or '').strip()
        self.meta_reply_blocked_reply_once = bool(self.config.get('meta_reply_blocked_reply_once', False))

        # 单用户最大回复轮数限制配置
        self.text_reply_limit_switch = bool(self.config.get('text_reply_limit_switch', False))
        self.text_reply_limit_count = self._coerce_int_range(self.config.get('text_reply_limit_count', 99), 99, 0, 99999)
        self.text_reply_limit_hours = self._coerce_int_range(self.config.get('text_reply_limit_hours', 24), 24, 0, 720)
        self.text_reply_limit_reply = self.config.get('text_reply_limit_reply', '')
        self.text_reply_limit_reply_once = bool(self.config.get('text_reply_limit_reply_once', False))
        self.text_reply_limit_ai_reply = bool(self.config.get('text_reply_limit_ai_reply', True))

        # 拆分多条回复配置
        self.chat_split_reply_switch  = bool(self.config.get('chat_split_reply_switch', False))
        self.chat_split_max_chars     = max(1, int(self.config.get('chat_split_max_chars', 100)))
        self.chat_split_max_count     = max(1, int(self.config.get('chat_split_max_count', 4)))
        self.group_split_reply_switch = bool(self.config.get('group_split_reply_switch', False))
        self.group_split_max_chars    = max(1, int(self.config.get('group_split_max_chars', 100)))
        self.group_split_max_count    = max(1, int(self.config.get('group_split_max_count', 4)))
        normalize_tts_settings(self.config)
        self.tts_configs = self.config.get('tts_configs', [])
        self.tts_index = max(0, int(self.config.get('tts_index', 0) or 0))
        self.chat_voice_reply_switch = bool(self.config.get('chat_voice_reply_switch', False))
        trigger_mode_source = self.config.get('chat_voice_reply_trigger_modes', None)
        self.chat_voice_reply_trigger_modes = [
            mode for mode in (str(item or '').strip() for item in (trigger_mode_source if trigger_mode_source is not None else ['keyword']))
            if mode in {'incoming_voice', 'keyword'}
        ]
        self.chat_voice_reply_request_keywords = self.config.get('chat_voice_reply_request_keywords', [])
        self.chat_voice_reply_cooldown_minutes = self._coerce_int_range(
            self.config.get('chat_voice_reply_cooldown_minutes', 10), 10, 0, 1440
        )
        self.chat_voice_reply_limit_count = self._coerce_int_range(
            self.config.get('chat_voice_reply_limit_count', 50), 50, 0, 99
        )
        self.chat_voice_reply_limit_hours = self._coerce_int_range(
            self.config.get('chat_voice_reply_limit_hours', 24), 24, 0, 720
        )
        self.chat_voice_session_minutes = self._coerce_int_range(
            self.config.get('chat_voice_session_minutes', 10), 10, 1, 1440
        )
        self.chat_voice_session_turns = self._coerce_int_range(
            self.config.get('chat_voice_session_turns', 5), 5, 1, 20
        )
        self.group_voice_reply_switch = bool(self.config.get('group_voice_reply_switch', False))
        self.group_voice_reply_request_keywords = self.config.get('group_voice_reply_request_keywords', [])
        self.group_voice_reply_cooldown_minutes = self._coerce_int_range(
            self.config.get('group_voice_reply_cooldown_minutes', 0), 0, 0, 1440
        )
        self.group_voice_reply_limit_count = self._coerce_int_range(
            self.config.get('group_voice_reply_limit_count', 99), 99, 0, 99
        )
        self.group_voice_reply_limit_hours = self._coerce_int_range(
            self.config.get('group_voice_reply_limit_hours', 24), 24, 0, 720
        )
        _siver_panel_defaults = {
            'siver_panel_enabled': False,
            'siver_panel_activation_code': '',
            'siver_panel_slug': '',
            'siver_panel_install_id': '',
            'siver_panel_machine_fingerprint': '',
            'siver_panel_device_id': '',
            'siver_panel_device_secret': '',
            'siver_panel_base_url': 'https://panel.siver.top',
            'siver_panel_ws_url': 'wss://panel.siver.top/relay/ws',
            'siver_panel_panel_url': '',
            'siver_panel_service_expire_at': '',
            'siver_panel_last_error_code': '',
            'siver_panel_last_error_message': '',
        }
        _siver_panel_needs_save = any(k not in self.config for k in _siver_panel_defaults)
        if self.config.get('siver_panel_base_url') == 'https://wxbot-panel.siverking.online':
            self.config['siver_panel_base_url'] = 'https://panel.siver.top'
            _siver_panel_needs_save = True
        if self.config.get('siver_panel_ws_url') == 'wss://wxbot-panel.siverking.online/relay/ws':
            self.config['siver_panel_ws_url'] = 'wss://panel.siver.top/relay/ws'
            _siver_panel_needs_save = True
        for k, v in _siver_panel_defaults.items():
            self.config.setdefault(k, v)
        if _siver_panel_needs_save:
            self.save_config()
            log(message='已自动补充 SiverPanel 远程访问配置默认值')

        log(message="全局配置更新完成")

    def set_config(self, id, new_content):
        """修改指定配置项并保存"""
        self.config[id] = new_content
        self.save_config()
        self.refresh_config()
        log(message=id + "已更改为:" + str(self.config[id]))

    # ----------------------------------------------------------
    # 监听用户管理
    # ----------------------------------------------------------

    def add_user(self, name):
        """将用户添加到当前模式对应的用户列表。"""
        list_key = 'global_blacklist' if self.AllListen_switch else 'listen_list'
        self.config.setdefault(list_key, [])
        if name not in self.config.get(list_key, []):
            self.config[list_key].append(name)
            self.save_config()
            self.refresh_config()
            log(message="添加后的用户列表:" + str(self.config[list_key]))
        else:
            log(message=f"用户 {name} 已在当前列表中")

    def remove_user(self, name):
        """从当前模式对应的用户列表中删除指定用户。"""
        list_key = 'global_blacklist' if self.AllListen_switch else 'listen_list'
        self.config.setdefault(list_key, [])
        if name in self.config.get(list_key, []):
            self.config[list_key].remove(name)
            self.save_config()
            self.refresh_config()
            log(message="删除后的用户列表:" + str(self.config[list_key]))
        else:
            log(message=f"用户 {name} 不在当前列表中")

    # ----------------------------------------------------------
    # 监听群组管理
    # ----------------------------------------------------------

    def add_group(self, name):
        """将群组添加到监听列表"""
        if name not in self.config.get('group', []):
            self.config['group'].append(name)
            self.save_config()
            self.refresh_config()
            log(message="添加后的监听群组列表:" + str(self.config['group']))
        else:
            log(message=f"群组 {name} 已在监听列表中")

    def remove_group(self, name):
        """从监听列表中删除指定群组"""
        if name in self.config.get('group', []):
            self.config['group'].remove(name)
            self.save_config()
            self.refresh_config()
            log(message="删除后的监听群组列表:" + str(self.config['group']))
        else:
            log(message=f"群组 {name} 不在监听列表中")

    def set_group_switch(self, switch_value):
        """设置群机器人总开关"""
        self.config['group_switch'] = switch_value
        self.save_config()
        self.refresh_config()
        log(message="群开关设置为" + str(self.config['group_switch']))

    # ----------------------------------------------------------
    # 工具方法（静态）
    # ----------------------------------------------------------

    @staticmethod
    def now_time(time_format="%Y/%m/%d %H:%M:%S "):
        """获取当前时间字符串（当前暂由公共 log 模块显示时间，此处返回空串）"""
        return ""  # 暂时采用公共类的 log 显示时间
        return datetime.now().strftime(time_format)

    @staticmethod
    def split_long_text(text, chunk_size=LONG_REPLY_SEGMENT_CHARS):
        """将超长文本按指定长度切分为列表，用于分段发送"""
        return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]

    @staticmethod
    def _normalize_chat_tts_map(raw_map):
        """清洗私聊白名单用户的专属 TTS 接口索引配置"""
        if not isinstance(raw_map, dict):
            return {}
        clean = {}
        for name, value in raw_map.items():
            name = str(name).strip()
            if not name:
                continue
            try:
                value = int(value)
            except Exception:
                continue
            clean[name] = max(0, value)
        return clean

    @staticmethod
    def _coerce_int_range(value, default, min_value, max_value):
        """将配置值转为指定范围内的整数"""
        return coerce_int_range(value, default, min_value, max_value)

    @staticmethod
    def _coerce_choice_int(value, default, choices):
        try:
            number = int(value)
        except Exception:
            number = int(default)
        return number if number in set(choices or []) else int(default)

    def human_delay(self, split_continuation=False):
        """模拟首条回复的人工操作随机延迟。"""
        if not self.reply_delay_switch:
            return
        lo = min(self.reply_delay_first_min, self.reply_delay_first_max)
        hi = max(self.reply_delay_first_min, self.reply_delay_first_max)
        time.sleep(random.randint(lo, hi))

    @staticmethod
    def get_run_time(start_time):
        """计算并返回自 start_time 至今的运行时长，格式：X天X时X分X秒"""
        delta = datetime.now() - start_time
        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{days}天{hours}时{minutes}分{seconds}秒"


# ============================================================
# 回复计数器管理类
# ============================================================

# ============================================================
# AI 接口类
# ============================================================

# ============================================================
# 微信机器人主类
# ============================================================
