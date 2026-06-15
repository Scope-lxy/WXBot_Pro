import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta

from core.chat_history_format import build_model_visible_history
from core.memory import resolve_memory_storage_name
from core.sending import sanitize_ai_output_text


INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MD_FENCE_RE = re.compile(r"```(?:markdown|md)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
UNKNOWN_PLACEHOLDER_RE = re.compile(r"{{\s*[a-zA-Z_][a-zA-Z0-9_]*\s*}}")
CONVERSATION_MEMORY_ANALYSIS_LIMIT = 200
CONVERSATION_MEMORY_DEFAULT_PROTECTED_RECENT_COUNT = 20
CONVERSATION_MEMORY_SCHEMA_VERSION = 1
CONVERSATION_MEMORY_MAX_PROFILE_ITEMS = 5
CONVERSATION_MEMORY_MAX_MEMORIES = 50
CONVERSATION_MEMORY_MAX_LINE_LENGTH = 180
CONVERSATION_MEMORY_USAGE_GUIDANCE = (
    "这些记忆用于理解对方近期状态、判断切入点和可延展话题。",
    "合适时可以自然带入，但不要逐条复述，也不要生硬拼接。",
    "如果与当前最新对话不一致，以当前对话为准。",
)
IMPORTANCE_LEVELS = {"高", "中", "低"}
ROOT_SECTION_TITLE = "会话记忆"
SECTION_TITLES = ("基础信息", "记忆条目")
PROFILE_LINE_RE = re.compile(r"^-\s*\[(B\d{2})\]\[([^\]]+)\]\s*(.+?)\s*$")
MEMORY_LINE_RE = re.compile(r"^-\s*\[(M\d{2})\]\[(高|中|低)\]\[([^\]]+)\]\s*(.+?)\s*$")
PERSONA_STATUS_SUFFIX = "-人设近况"
PERSONA_STATUS_HEADING_RE = re.compile(r"^\s*#\s+人设近况\s*$", re.MULTILINE)
SYSTEM_PROMPT_BACKUP_DIR = "prompt_backup"


def app_base_dir():
    return os.path.dirname(sys.executable) if hasattr(sys, "_MEIPASS") else os.path.abspath(".")


class SystemPromptStore:
    """Load software-owned prompts and restore them from a shared backup directory."""

    def __init__(self, prompt_dir=None):
        self.prompt_dir = prompt_dir or os.path.join(app_base_dir(), "data", "system_prompts")

    def load(self, filename, required_placeholders=()):
        path = os.path.join(self.prompt_dir, filename)
        backup_path = self._backup_path(filename)
        text = self._read_valid_prompt(path, required_placeholders)
        if text:
            return text
        backup_text = self._read_valid_prompt(backup_path, required_placeholders)
        if not backup_text:
            raise FileNotFoundError(f"系统 Prompt 文件不可用且备份不可恢复：{filename}")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copyfile(backup_path, path)
        return backup_text

    def _read_valid_prompt(self, path, required_placeholders):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read().strip()
        except Exception:
            return ""
        if not text:
            return ""
        for placeholder in required_placeholders:
            if placeholder not in text:
                return ""
        return text

    def render(self, filename, values, required_placeholders=()):
        template = self.load(filename, required_placeholders=required_placeholders)
        rendered = self._render_text(template, values)
        if not UNKNOWN_PLACEHOLDER_RE.search(rendered):
            return rendered

        path = os.path.join(self.prompt_dir, filename)
        backup_path = self._backup_path(filename)
        backup_text = self._read_valid_prompt(backup_path, required_placeholders)
        if backup_text and backup_text != template:
            backup_rendered = self._render_text(backup_text, values)
            if not UNKNOWN_PLACEHOLDER_RE.search(backup_rendered):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                shutil.copyfile(backup_path, path)
                return backup_rendered
        raise ValueError(f"系统 Prompt 渲染后仍包含未知占位符：{filename}")

    def _backup_path(self, filename):
        backup_dir = os.path.join(self.prompt_dir, SYSTEM_PROMPT_BACKUP_DIR)
        return os.path.join(backup_dir, filename)

    @staticmethod
    def _render_text(template, values):
        rendered = template
        for key, value in (values or {}).items():
            rendered = rendered.replace("{{" + str(key) + "}}", "" if value is None else str(value))
        return rendered


class ConversationMemoryStore:
    """Persist conversation memory as JSON while keeping Markdown parse/render helpers."""

    def __init__(self, base_path):
        self.base_path = base_path
        os.makedirs(self.base_path, exist_ok=True)

    def safe_chat_name(self, chat_name):
        safe_name = INVALID_FILENAME_RE.sub("_", str(chat_name or "").strip())
        safe_name = safe_name.strip(". ")
        return safe_name or "unknown"

    def state_storage_name(self, chat_name):
        return resolve_memory_storage_name(chat_name)

    def state_path(self, chat_name):
        return os.path.join(self.base_path, f"{self.state_storage_name(chat_name)}.json")

    def default_document(self, chat_name):
        return self.render_document(self.default_state(chat_name))

    def default_state(self, chat_name, prompt_name="", wx_id=""):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state = {
            "schema_version": 2,
            "wx_id": str(wx_id or "").strip(),
            "chat_name": str(chat_name or "").strip(),
            "updated_at": now,
            "memories": [],
            "maintenance": {
                "last_processed_message_key": "",
                "last_processed_message_time": "",
                "last_processed_at": "",
                "last_attempted_at": "",
            },
        }
        return self.normalize_state(state, chat_name=chat_name, wx_id=wx_id, keep_updated_at=True)

    def load_state(self, chat_name, prompt_name="", wx_id="", strict=False):
        path = self.state_path(chat_name)
        if not os.path.exists(path):
            return self.default_state(chat_name, prompt_name, wx_id=wx_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw_state = json.load(f)
            return self.normalize_state(raw_state, chat_name=chat_name, wx_id=wx_id or raw_state.get("wx_id", ""), keep_updated_at=True)
        except Exception as exc:
            if strict:
                raise ValueError("会话记忆文件已损坏，无法读取；请先修复或删除后重建。") from exc
            return self.default_state(chat_name, prompt_name, wx_id=wx_id)

    def save_state(self, chat_name, state, wx_id=""):
        if not isinstance(state, dict):
            raise ValueError("会话记忆必须使用结构化 JSON 状态保存")
        if os.path.exists(self.state_path(chat_name)):
            self.load_state(chat_name, wx_id=wx_id, strict=True)
        normalized = self.normalize_state(state, chat_name=chat_name, wx_id=wx_id)
        ok, message = self.validate_state(normalized)
        if not ok:
            raise ValueError(message)
        self._raise_on_unexpected_clear(chat_name, normalized)
        with open(self.state_path(chat_name), "w", encoding="utf-8") as f:
            json.dump(self._state_to_json_payload(normalized), f, ensure_ascii=False, indent=2)
        return normalized

    def save_document(self, chat_name, document, maintenance=None, wx_id=""):
        document = self.normalize_document(document, chat_name, maintenance=maintenance)
        ok, message = self.validate_document(document)
        if not ok:
            raise ValueError(message)
        state = self.parse_document(document, chat_name)
        if isinstance(maintenance, dict):
            state.setdefault("maintenance", {})
            state["maintenance"].update({
                key: str(maintenance.get(key, state["maintenance"].get(key, "")) or "").strip()
                for key in ("last_processed_message_key", "last_processed_message_time", "last_processed_at", "last_attempted_at")
            })
        return self.save_state(chat_name, state, wx_id=wx_id)

    def delete_state(self, chat_name):
        path = self.state_path(chat_name)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    def _load_existing_state_for_guard(self, chat_name):
        path = self.state_path(chat_name)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw_state = json.load(f)
            return self.normalize_state(raw_state, chat_name=chat_name, wx_id=raw_state.get("wx_id", ""), keep_updated_at=True)
        except Exception:
            return None

    @staticmethod
    def _state_has_profile_items(state):
        profile = (state.get("profile") or {}) if isinstance(state, dict) else {}
        items = profile.get("items") if isinstance(profile, dict) else []
        return bool(items)

    @staticmethod
    def _state_has_memories(state):
        memories = state.get("memories") if isinstance(state, dict) else []
        return bool(memories)

    def _raise_on_unexpected_clear(self, chat_name, incoming_state):
        current_state = self._load_existing_state_for_guard(chat_name)
        if not current_state:
            return
        current_memory_count = len(current_state.get("memories") or []) if isinstance(current_state, dict) else 0
        incoming_memory_count = len(incoming_state.get("memories") or []) if isinstance(incoming_state, dict) else 0
        if current_memory_count > 0 and incoming_memory_count == 0:
            raise ValueError("为防止误清空，已有会话记忆不能直接保存为空；如需清空请使用删除功能。")
        if current_memory_count >= 5 and incoming_memory_count > 0 and incoming_memory_count * 2 < current_memory_count:
            raise ValueError("为防止误覆盖，已有会话记忆出现异常缩水，本次保存已被拦截。")

    def list_states(self):
        states = []
        if not os.path.isdir(self.base_path):
            return states
        for name in sorted(os.listdir(self.base_path)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.base_path, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw_state = json.load(f)
                states.append(self.normalize_state(raw_state, chat_name=name[:-5], wx_id=raw_state.get("wx_id", ""), keep_updated_at=True))
            except Exception:
                pass
        return states

    def normalize_state(self, state, chat_name="", wx_id="", keep_updated_at=False):
        state = state if isinstance(state, dict) else {}
        current_time = str(state.get("updated_at") or "").strip() if keep_updated_at else ""
        if not current_time:
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        normalized_memories = self._normalize_structured_items(
            self._memory_items_from_any_state(state),
            kind="memory",
            limit=CONVERSATION_MEMORY_MAX_MEMORIES,
            default_timestamp=current_time,
        )
        normalized_profile_items = self._normalize_structured_items(
            self._profile_items_from_any_state(state),
            kind="profile",
            limit=CONVERSATION_MEMORY_MAX_PROFILE_ITEMS,
            default_timestamp=current_time,
        )
        unified_memories = [
            self._profile_item_to_memory_item(item)
            for item in normalized_profile_items
        ] + list(normalized_memories)
        unified_memories.sort(key=self._memory_sort_key)
        normalized = {
            "schema_version": 2,
            "wx_id": str(wx_id or state.get("wx_id", "") or "").strip(),
            "chat_name": str(chat_name or state.get("chat_name", "") or "").strip(),
            "updated_at": current_time,
            "memories": unified_memories,
            "maintenance": {
                "last_processed_message_key": str(((state.get("maintenance") or {}).get("last_processed_message_key", "")) or "").strip(),
                "last_processed_message_time": str(((state.get("maintenance") or {}).get("last_processed_message_time", "")) or "").strip(),
                "last_processed_at": str(((state.get("maintenance") or {}).get("last_processed_at", "")) or "").strip(),
                "last_attempted_at": str(((state.get("maintenance") or {}).get("last_attempted_at", "")) or "").strip(),
            },
        }
        normalized["document"] = self._render_state_document(normalized.get("chat_name", ""), normalized)
        return normalized

    def validate_state(self, state):
        state = self.normalize_state(state, chat_name=state.get("chat_name", "") if isinstance(state, dict) else "", wx_id=state.get("wx_id", "") if isinstance(state, dict) else "", keep_updated_at=True)
        memories = list(state.get("memories", [])) if isinstance(state.get("memories"), list) else []
        if len(memories) > CONVERSATION_MEMORY_MAX_MEMORIES:
            return False, f"会话记忆最多保留 {CONVERSATION_MEMORY_MAX_MEMORIES} 条"
        ids = []
        for item in memories:
            item_id = str(item.get("id", "") or "").strip()
            if item_id:
                ids.append(item_id)
            item_type = str(item.get("type", "") or item.get("category", "") or "").strip()
            content = str(item.get("content", "") or "").strip()
            if not item_type or not content:
                return False, "会话记忆条目的类型和内容不能为空"
            if "importance" in item and item.get("importance") not in IMPORTANCE_LEVELS:
                return False, "记忆条目的重要度必须是高、中、低"
        if len(ids) != len(set(ids)):
            return False, "会话记忆存在重复 ID"
        return True, ""

    def render_document(self, state):
        state = self.normalize_state(state, chat_name=state.get("chat_name", "") if isinstance(state, dict) else "", wx_id=state.get("wx_id", "") if isinstance(state, dict) else "", keep_updated_at=True)
        return self._render_state_document(state.get("chat_name", ""), state)

    def _state_to_json_payload(self, state):
        state = self.normalize_state(state, chat_name=state.get("chat_name", "") if isinstance(state, dict) else "", wx_id=state.get("wx_id", "") if isinstance(state, dict) else "", keep_updated_at=True)
        return {
            "schema_version": 2,
            "wx_id": state.get("wx_id", ""),
            "chat_name": state.get("chat_name", ""),
            "updated_at": state.get("updated_at", ""),
            "maintenance": dict(state.get("maintenance", {}) or {}),
            "memories": [
                {
                    "id": item.get("id", ""),
                    "importance": item.get("importance", "中"),
                    "type": item.get("type", "") or item.get("category", ""),
                    "content": item.get("content", ""),
                    "created_at": item.get("created_at", ""),
                    "updated_at": item.get("updated_at", ""),
                }
                for item in state.get("memories", []) or []
            ],
        }

    @staticmethod
    def _profile_items_from_any_state(state):
        if not isinstance(state, dict):
            return []
        profile = state.get("profile")
        if isinstance(profile, dict):
            items = profile.get("items")
            if isinstance(items, list):
                return items
        if isinstance(profile, list):
            return profile
        return []

    @staticmethod
    def _memory_items_from_any_state(state):
        if not isinstance(state, dict):
            return []
        memories = state.get("memories")
        if not isinstance(memories, list):
            return []
        return list(memories)

    def _normalize_structured_items(self, items, kind, limit, default_timestamp=""):
        out = []
        seen = set()
        prefix = "M" if kind == "memory" else "B"
        next_num = 1
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict):
                item_id = str(item.get("id", "") or "").strip()
                item_type = str(item.get("type", "") or item.get("category", "") or "").strip()
                content = str(item.get("content", "") or item.get("text", "") or "").strip()
                importance = str(item.get("importance", "") or "中").strip()
                created_at = str(item.get("created_at", "") or "").strip()
                updated_at = str(item.get("updated_at", "") or "").strip()
            else:
                item_id = ""
                item_type = "记录"
                content = str(item or "").strip()
                importance = "中"
                created_at = ""
                updated_at = ""
            if not content:
                continue
            content = self._clean_line_text(content)
            if self._is_placeholder_line(content):
                continue
            item_type = self._clean_line_text(item_type or "记录")
            importance = importance if importance in IMPORTANCE_LEVELS else "中"
            updated_at = updated_at or default_timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            created_at = created_at or updated_at
            if not re.match(rf"^{prefix}\d{{2}}$", item_id):
                item_id = ""
            while not item_id or item_id in seen:
                item_id = f"{prefix}{next_num:02d}"
                next_num += 1
            seen.add(item_id)
            payload = {
                "id": item_id,
                "type": item_type,
                "category": item_type,
                "content": content,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            if kind == "memory":
                payload["importance"] = importance
            out.append(payload)
            if len(out) >= limit:
                break
        return out

    def _profile_item_to_memory_item(self, item):
        return {
            "id": str(item.get("id", "") or "").strip(),
            "importance": "高",
            "type": str(item.get("type", "") or item.get("category", "") or "基础信息").strip(),
            "category": str(item.get("category", "") or item.get("type", "") or "基础信息").strip(),
            "content": str(item.get("content", "") or "").strip(),
            "created_at": str(item.get("created_at", "") or "").strip(),
            "updated_at": str(item.get("updated_at", "") or "").strip(),
        }

    @staticmethod
    def _is_profile_memory_item(item):
        if not isinstance(item, dict):
            return False
        return bool(re.match(r"^B\d{2}$", str(item.get("id", "") or "").strip()))

    def _split_state_memories(self, state):
        memories = state.get("memories", []) if isinstance(state, dict) else []
        profile_items = []
        regular_memories = []
        for item in memories if isinstance(memories, list) else []:
            if self._is_profile_memory_item(item):
                profile_items.append(item)
            else:
                regular_memories.append(item)
        return profile_items, regular_memories

    def _memory_sort_key(self, item):
        importance_order = {"高": 0, "中": 1, "低": 2}
        item = item if isinstance(item, dict) else {}
        updated_at = self._parse_state_time(item.get("updated_at"))
        created_at = self._parse_state_time(item.get("created_at"))
        if updated_at is None:
            updated_ts = float("inf")
        else:
            updated_ts = -updated_at.timestamp()
        if created_at is None:
            created_ts = float("inf")
        else:
            created_ts = -created_at.timestamp()
        return (
            importance_order.get(str(item.get("importance", "") or "").strip(), 1),
            updated_ts,
            created_ts,
            str(item.get("id", "") or "").strip(),
        )

    def _parse_state_time(self, value):
        if isinstance(value, datetime):
            return value
        value = str(value or "").strip()
        if not value:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
            try:
                return datetime.strptime(value, fmt)
            except Exception:
                pass
        return None

    def merge_proposal(self, current_state, proposal, chat_name="", wx_id="", now=None):
        current_state = self.normalize_state(current_state, chat_name=chat_name or (current_state.get("chat_name", "") if isinstance(current_state, dict) else ""), wx_id=wx_id or (current_state.get("wx_id", "") if isinstance(current_state, dict) else ""), keep_updated_at=True)
        proposal = proposal if isinstance(proposal, dict) else {}
        merged = {
            "schema_version": 2,
            "wx_id": str(wx_id or current_state.get("wx_id", "") or "").strip(),
            "chat_name": str(chat_name or current_state.get("chat_name", "") or "").strip(),
            "updated_at": str(now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "maintenance": dict(current_state.get("maintenance", {}) or {}),
            "memories": list(current_state.get("memories", []) or []),
        }
        memories_by_id = {
            str(item.get("id", "") or ""): dict(item)
            for item in merged.get("memories", []) or []
            if str(item.get("id", "") or "").strip()
        }
        ordered_ids = [str(item.get("id", "") or "") for item in merged.get("memories", []) or [] if str(item.get("id", "") or "").strip()]
        delete_ids = []
        allow_delete = len(self._memory_items_from_any_state(merged)) >= 5
        for raw in proposal.get("delete", []) or []:
            if isinstance(raw, dict):
                item_id = str(raw.get("id", "") or "").strip()
            else:
                item_id = str(raw or "").strip()
            if allow_delete and item_id:
                delete_ids.append(item_id)
        for item_id in delete_ids:
            memories_by_id.pop(item_id, None)
        ordered_ids = [item_id for item_id in ordered_ids if item_id not in set(delete_ids)]
        for item in proposal.get("update", []) or []:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id", "") or "").strip()
            if not item_id or item_id not in memories_by_id:
                continue
            normalized_items = self._normalize_structured_items(
                [item],
                kind="memory",
                limit=1,
                default_timestamp=merged["updated_at"],
            )
            if normalized_items:
                normalized = normalized_items[0]
                normalized["id"] = item_id
                normalized["created_at"] = str(memories_by_id[item_id].get("created_at", "") or normalized.get("created_at", "") or "").strip()
                normalized["updated_at"] = merged["updated_at"]
                memories_by_id[item_id] = normalized
                if item_id not in ordered_ids:
                    ordered_ids.append(item_id)
        existing_ids = set(memories_by_id.keys()) | set(ordered_ids)
        next_num = 1
        add_items = []
        for item in proposal.get("add", []) or []:
            if not isinstance(item, dict):
                continue
            normalized_items = self._normalize_structured_items(
                [item],
                kind="memory",
                limit=1,
                default_timestamp=merged["updated_at"],
            )
            if not normalized_items:
                continue
            normalized = normalized_items[0]
            new_id = str(normalized.get("id", "") or "").strip()
            while not new_id or new_id in existing_ids:
                new_id = f"M{next_num:02d}"
                next_num += 1
            normalized["id"] = new_id
            normalized["created_at"] = merged["updated_at"]
            normalized["updated_at"] = merged["updated_at"]
            existing_ids.add(new_id)
            add_items.append(normalized)
        merged["memories"] = [memories_by_id[item_id] for item_id in ordered_ids if item_id in memories_by_id] + add_items
        return self.normalize_state(merged, chat_name=merged.get("chat_name", ""), wx_id=merged.get("wx_id", ""), keep_updated_at=True)

    def parse_document(self, document, chat_name=""):
        document = self._strip_code_fence(str(document or "")).strip()
        meta, body = self._split_frontmatter(document)
        sections = self._parse_sections(body)
        profile_items = self._section_items(sections.get("基础信息", ""), "profile")
        preferred_name, aliases = self._profile_name_fields(profile_items)
        return {
            "chat_name": str(meta.get("chat_name") or chat_name or ""),
            "updated_at": str(meta.get("updated_at") or ""),
            "profile": {
                "preferred_name": preferred_name,
                "aliases": aliases,
                "items": profile_items,
            },
            "memories": self._section_items(sections.get("记忆条目", ""), "memory"),
            "maintenance": {
                "last_processed_message_key": str(meta.get("last_processed_message_key") or "").strip(),
                "last_processed_message_time": str(meta.get("last_processed_message_time") or "").strip(),
                "last_processed_at": str(meta.get("last_processed_at") or "").strip(),
                "last_attempted_at": str(meta.get("last_attempted_at") or "").strip(),
            },
            "document": document or self.default_document(chat_name),
        }

    def normalize_document(self, document, chat_name="", maintenance=None):
        document = self._strip_code_fence(str(document or "")).strip()
        errors = self._body_shape_errors(document)
        if errors:
            raise ValueError("；".join(errors))
        state = self.parse_document(document, chat_name)
        if isinstance(maintenance, dict):
            state["maintenance"].update({
                key: str(maintenance.get(key, state["maintenance"].get(key, "")) or "").strip()
                for key in ("last_processed_message_key", "last_processed_message_time", "last_processed_at", "last_attempted_at")
            })
        return self._render_state_document(chat_name, state)

    def _render_state_document(self, chat_name, state):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state = state if isinstance(state, dict) else self.default_state(chat_name)
        state.setdefault("maintenance", {})
        body = self._normalize_body(state)
        return (
            "---\n"
            f"schema_version: {CONVERSATION_MEMORY_SCHEMA_VERSION}\n"
            f"chat_name: {chat_name or state.get('chat_name', '')}\n"
            f"updated_at: {now}\n"
            f"last_processed_message_key: {state['maintenance'].get('last_processed_message_key', '')}\n"
            f"last_processed_message_time: {state['maintenance'].get('last_processed_message_time', '')}\n"
            f"last_processed_at: {state['maintenance'].get('last_processed_at', '')}\n"
            f"last_attempted_at: {state['maintenance'].get('last_attempted_at', '')}\n"
            "---\n\n"
            f"{body}\n"
        )

    def validate_document(self, document):
        document = self._strip_code_fence(str(document or "")).strip()
        if not document.startswith("---"):
            return False, "会话记忆 MD 必须包含 frontmatter"
        if not self._has_closed_frontmatter(document):
            return False, "会话记忆 MD frontmatter 未正确闭合"
        shape_errors = self._body_shape_errors(document)
        if shape_errors:
            return False, "；".join(shape_errors)
        _, body = self._split_frontmatter(document)
        sections = self._parse_sections(body)
        missing = [title for title in SECTION_TITLES if title not in sections]
        if missing:
            return False, f"会话记忆 MD 缺少区块：{', '.join(missing)}"
        invalid_profile_lines = self._invalid_section_lines(sections.get("基础信息", ""), "profile")
        if invalid_profile_lines:
            return False, "基础信息 MD 存在不符合格式的条目"
        invalid_lines = self._invalid_section_lines(sections.get("记忆条目", ""), "memory")
        if invalid_lines:
            return False, "会话记忆 MD 存在不符合格式的条目"
        state = self.parse_document(document)
        if len(state["profile"].get("items", [])) > CONVERSATION_MEMORY_MAX_PROFILE_ITEMS:
            return False, f"基础信息最多保留 {CONVERSATION_MEMORY_MAX_PROFILE_ITEMS} 条"
        if len(state["memories"]) > CONVERSATION_MEMORY_MAX_MEMORIES:
            return False, f"会话记忆最多保留 {CONVERSATION_MEMORY_MAX_MEMORIES} 条"
        ids = [
            item["id"]
            for item in [*state["profile"].get("items", []), *state["memories"]]
            if item.get("id")
        ]
        if len(ids) != len(set(ids)):
            return False, "会话记忆 MD 存在重复 ID"
        return True, ""

    def _body_shape_errors(self, document):
        document = str(document or "").strip()
        if document.startswith("---") and not self._has_closed_frontmatter(document):
            return ["会话记忆 MD frontmatter 未正确闭合"]
        _, body = self._split_frontmatter(document)
        has_root, sections, unknown_headings = self._parse_body_structure(body)
        missing = [title for title in SECTION_TITLES if title not in sections]
        errors = []
        if not has_root:
            errors.append(f"会话记忆 MD 缺少区块：{ROOT_SECTION_TITLE}")
        if missing:
            errors.append(f"会话记忆 MD 缺少区块：{', '.join(missing)}")
        if unknown_headings:
            errors.append(f"会话记忆 MD 包含不支持的区块：{', '.join(unknown_headings)}")
        invalid_profile_lines = self._invalid_section_lines(sections.get("基础信息", ""), "profile")
        if invalid_profile_lines:
            errors.append("基础信息 MD 存在不符合格式的条目")
        invalid_lines = self._invalid_section_lines(sections.get("记忆条目", ""), "memory")
        if invalid_lines:
            errors.append("会话记忆 MD 存在不符合格式的条目")
        return errors

    @staticmethod
    def is_allowed_path(path):
        return str(path or "").strip() in {"基础信息", "记忆条目"}

    def _normalize_body(self, state):
        profile_lines = self._normalize_items(self._profile_items_from_any_state(state), "profile", CONVERSATION_MEMORY_MAX_PROFILE_ITEMS)
        memories = self._normalize_items(self._memory_items_from_any_state(state), "memory", CONVERSATION_MEMORY_MAX_MEMORIES)
        return "\n".join([
            "# 会话记忆",
            "",
            "## 基础信息",
            "",
            *profile_lines,
            *([""] if profile_lines else []),
            "## 记忆条目",
            "",
            *memories,
        ]).rstrip()

    def _normalize_items(self, items, kind, limit):
        out = []
        seen = set()
        prefix = "M" if kind == "memory" else "B"
        next_num = 1
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict):
                item_id = str(item.get("id", "") or "").strip()
                importance = str(item.get("importance", "") or "中").strip()
                category = str(item.get("category", "") or "记录").strip()
                content = str(item.get("content", "") or item.get("text", "") or "").strip()
            else:
                item_id = ""
                importance = "中"
                category = "记录"
                content = str(item or "").strip()
            if not content:
                continue
            importance = importance if importance in IMPORTANCE_LEVELS else "中"
            category = category or "记录"
            content = self._clean_line_text(content)
            if self._is_placeholder_line(content):
                continue
            if not re.match(rf"^{prefix}\d{{2}}$", item_id):
                item_id = ""
            while not item_id or item_id in seen:
                item_id = f"{prefix}{next_num:02d}"
                next_num += 1
            seen.add(item_id)
            if kind == "profile":
                out.append(f"- [{item_id}][{category}] {content}")
            else:
                out.append(f"- [{item_id}][{importance}][{category}] {content}")
            if len(out) >= limit:
                break
        return out

    def _profile_items_from_state(self, profile):
        if not isinstance(profile, dict):
            return []
        items = profile.get("items")
        if isinstance(items, list):
            return items
        fallback = []
        preferred_name = str(profile.get("preferred_name", "") or "").strip()
        aliases = profile.get("aliases", [])
        if preferred_name:
            fallback.append({"category": "称呼", "content": preferred_name})
        if isinstance(aliases, list):
            aliases_text = "、".join(str(item).strip() for item in aliases if str(item).strip())
        else:
            aliases_text = str(aliases or "").strip()
        if aliases_text:
            fallback.append({"category": "昵称", "content": aliases_text})
        return fallback

    def _profile_name_fields(self, items):
        preferred_name = ""
        aliases = []
        for item in items if isinstance(items, list) else []:
            category = str(item.get("category", "") or "").strip()
            content = str(item.get("content", "") or "").strip()
            if not content:
                continue
            if category in {"称呼", "姓名"} and not preferred_name:
                preferred_name = content
            elif category in {"昵称", "其他称呼"}:
                aliases.extend([
                    part.strip()
                    for part in re.split(r"[、,，/]", content)
                    if part.strip() and not self._is_placeholder_line(part)
                ])
        return preferred_name, aliases

    def _section_items(self, section_text, kind):
        items = []
        for line in str(section_text or "").splitlines():
            line = line.strip()
            if not line or line.startswith("<!--"):
                continue
            if not line.startswith("-"):
                continue
            if kind == "profile":
                match = PROFILE_LINE_RE.match(line)
                if match:
                    content = self._clean_line_text(match.group(3))
                    if not self._is_placeholder_line(content):
                        items.append({
                            "id": match.group(1),
                            "category": match.group(2).strip(),
                            "content": content,
                        })
                    continue
                content = self._clean_line_text(self._strip_memory_tag_prefix(line.lstrip("-").strip()))
                if content and not self._is_placeholder_line(content):
                    items.append({
                        "id": "",
                        "category": "记录",
                        "content": content,
                    })
                continue
            match = MEMORY_LINE_RE.match(line)
            if match:
                content = self._clean_line_text(match.group(4))
                if self._is_placeholder_line(content):
                    continue
                items.append({
                    "id": match.group(1),
                    "importance": match.group(2),
                    "category": match.group(3).strip(),
                    "content": content,
                })
                continue
            content = self._clean_line_text(self._strip_memory_tag_prefix(line.lstrip("-").strip()))
            if content and not self._is_placeholder_line(content):
                items.append({
                    "id": "",
                    "importance": "中",
                    "category": "记录",
                    "content": content,
                })
        return items

    def _invalid_section_lines(self, section_text, kind):
        invalid = []
        for line in str(section_text or "").splitlines():
            line = line.strip()
            if not line or line.startswith("<!--"):
                continue
            if not line.startswith("-"):
                invalid.append(line)
                continue
            if kind == "profile":
                match = PROFILE_LINE_RE.match(line)
            else:
                match = MEMORY_LINE_RE.match(line)
            if not match:
                invalid.append(line)
        return invalid

    def _parse_body_structure(self, body):
        root_found = False
        sections = {}
        unknown_headings = []
        current = None
        buf = []
        for line in str(body or "").splitlines():
            root_match = re.match(r"^#\s+(.+?)\s*$", line)
            section_match = re.match(r"^##\s+(.+?)\s*$", line)
            other_heading = re.match(r"^(#{3,6})\s+(.+?)\s*$", line)
            if root_match:
                if current:
                    sections[current] = "\n".join(buf).strip()
                    current = None
                    buf = []
                title = root_match.group(1).strip()
                if title == ROOT_SECTION_TITLE and not root_found:
                    root_found = True
                else:
                    unknown_headings.append(title)
            elif section_match:
                if current:
                    sections[current] = "\n".join(buf).strip()
                title = section_match.group(1).strip()
                if root_found and title in SECTION_TITLES and title not in sections:
                    current = title
                    buf = []
                else:
                    unknown_headings.append(title)
                    current = None
                    buf = []
            elif other_heading:
                unknown_headings.append(other_heading.group(2).strip())
                if current:
                    buf.append(line)
            elif current:
                buf.append(line)
        if current:
            sections[current] = "\n".join(buf).strip()
        return root_found, sections, unknown_headings

    def _parse_sections(self, body):
        return self._parse_body_structure(body)[1]

    def _split_frontmatter(self, document):
        lines = str(document or "").splitlines()
        if lines and lines[0].strip() == "---":
            for idx in range(1, len(lines)):
                if lines[idx].strip() == "---":
                    return self._parse_frontmatter(lines[1:idx]), "\n".join(lines[idx + 1:]).strip()
        return {}, str(document or "").strip()

    def _has_closed_frontmatter(self, document):
        lines = str(document or "").splitlines()
        return bool(lines and lines[0].strip() == "---" and any(line.strip() == "---" for line in lines[1:]))

    def _parse_frontmatter(self, lines):
        meta = {}
        for line in lines:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
        return meta

    def _strip_code_fence(self, text):
        match = MD_FENCE_RE.search(text)
        return match.group(1).strip() if match else text

    def _clean_line_text(self, text):
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        return text[:CONVERSATION_MEMORY_MAX_LINE_LENGTH].rstrip()

    def _is_placeholder_line(self, text):
        normalized = re.sub(r"[\s。.!！?？,，、；;：:]+", "", str(text or "")).strip()
        return normalized in {
            "暂无",
            "无",
            "无待补充",
            "待补充",
            "没有",
            "无内容",
            "暂无内容",
            "暂无会话记忆",
            "暂无记忆",
            "没有可沉淀信息",
            "没有可沉淀的长期信息",
            "没有会话记忆",
        }

    def _strip_memory_tag_prefix(self, text):
        text = str(text or "").strip()
        if not re.match(r"^\[(?:[BM]\d{1,3}|重要度:|[高中低]\])", text):
            return text
        text = re.sub(r"^\[[BM]\d{1,3}\]\s*", "", text)
        text = re.sub(r"^\[(?:高|中|低)\]\s*", "", text)
        text = re.sub(r"^\[重要度:[^\]]+\]\s*", "", text)
        text = re.sub(r"^\[[^\]]+\]\s*", "", text)
        text = re.sub(r"^\[[^\]\s]{1,20}\s+", "", text)
        return text.strip()


class PromptBuilder:
    """Compose the final system prompt from the prompt template and conversation memory."""

    FINAL_PROMPT_FILE = "final_reply.md"

    def __init__(self, prompt_store=None):
        self.prompt_store = prompt_store or SystemPromptStore()

    def build(
        self,
        chat_name,
        base_prompt,
        state,
        current_message="",
        now=None,
        image_parse_block="",
        persona_status_block="",
    ):
        now = now or datetime.now().strftime("%Y-%m-%d %H:%M")
        conversation_memory_section = self.build_conversation_memory_section(state)
        return self.prompt_store.render(
            self.FINAL_PROMPT_FILE,
            {
                "base_prompt": str(base_prompt or "").strip(),
                "now": now,
                "chat_name": chat_name,
                "conversation_memory_section": conversation_memory_section,
                "persona_status_block": str(persona_status_block or "").strip(),
                "image_parse_block": str(image_parse_block or "").strip(),
            },
            required_placeholders=(
                "{{base_prompt}}",
                "{{now}}",
                "{{conversation_memory_section}}",
                "{{persona_status_block}}",
                "{{image_parse_block}}",
            ),
        ).strip()

    def build_conversation_memory_section(self, state):
        summary_lines = self.state_summary(state)
        if not summary_lines:
            return ""
        lines = [
            "# 会话记忆",
            "",
            *CONVERSATION_MEMORY_USAGE_GUIDANCE,
            "",
            *summary_lines,
        ]
        return "\n\n" + "\n".join(lines).rstrip()

    def state_summary(self, state):
        if not isinstance(state, dict):
            return []
        raw_memories = state.get("memories")
        raw_memories = raw_memories if isinstance(raw_memories, list) else []
        has_flattened_profile = any(
            ConversationMemoryStore._is_profile_memory_item(item)
            for item in raw_memories
            if isinstance(item, dict)
        )
        if has_flattened_profile or not ConversationMemoryStore._profile_items_from_any_state(state):
            source_items = list(raw_memories)
        else:
            source_items = []
            for item in ConversationMemoryStore._profile_items_from_any_state(state)[:CONVERSATION_MEMORY_MAX_PROFILE_ITEMS]:
                if isinstance(item, dict):
                    source_items.append({
                        "id": str(item.get("id", "") or "").strip(),
                        "importance": "高",
                        "type": str(item.get("type", "") or item.get("category", "") or "").strip(),
                        "category": str(item.get("category", "") or item.get("type", "") or "").strip(),
                        "content": str(item.get("content", "") or "").strip(),
                    })
            source_items.extend(
                item for item in ConversationMemoryStore._memory_items_from_any_state(state)[:CONVERSATION_MEMORY_MAX_MEMORIES]
                if isinstance(item, dict)
            )
        summary_lines = []
        for item in source_items:
            content = str(item.get("content", "") or "").strip()
            category = str(item.get("category", "") or item.get("type", "") or "").strip()
            if content:
                summary_lines.append("- " + self._summary_sentence(f"{category}：{content}" if category else content))
        return summary_lines

    def _summary_sentence(self, text):
        text = str(text or "").strip()
        if not text:
            return ""
        return text if text[-1:] in "。！？!?" else text + "。"


class ConversationMemoryExtractor:
    """Decide when to maintain conversation memory and ask the AI for a structured proposal JSON."""

    EXTRACT_PROMPT_FILE = "conversation_memory_extract.md"
    REPAIR_PROMPT_FILE = "conversation_memory_repair.md"
    EXTRACTION_TASK_MESSAGE = "请根据以上聊天记录和当前记忆，输出提案 JSON。"

    def __init__(
        self,
        message_threshold=100,
        interval_hours=12,
        protected_recent_count=0,
        analysis_limit=CONVERSATION_MEMORY_ANALYSIS_LIMIT,
        prompt_store=None,
    ):
        self.message_threshold = max(1, int(message_threshold))
        self.interval_hours = max(1, int(interval_hours))
        self.protected_recent_count = max(0, int(protected_recent_count))
        self.analysis_limit = max(1, int(analysis_limit))
        self.prompt_store = prompt_store or SystemPromptStore()

    def should_extract(self, state, messages, protected_count=None, now=None):
        new_messages = self._messages_after_checkpoint(state, messages, protected_count=protected_count)
        if not new_messages:
            return False
        if len(new_messages) >= self.message_threshold:
            return True
        maintenance = (state.get("maintenance") or {}) if isinstance(state, dict) else {}
        last_at = self._parse_time(maintenance.get("last_processed_at"))
        current = self._parse_time(now) or datetime.now()
        if last_at is None:
            return False
        return current - last_at >= timedelta(hours=self.interval_hours)

    def build_extraction_prompt(self, state):
        current_document = json.dumps(self.proposal_context(state), ensure_ascii=False, indent=2)
        return self.prompt_store.render(
            self.EXTRACT_PROMPT_FILE,
            {
                "current_memory_body": current_document,
            },
            required_placeholders=("{{current_memory_body}}",),
        )

    def build_repair_prompt(self, bad_output):
        return self.prompt_store.render(
            self.REPAIR_PROMPT_FILE,
            {"bad_output": str(bad_output or "").strip()},
            required_placeholders=("{{bad_output}}",),
        )

    def proposal_context(self, state):
        state = state if isinstance(state, dict) else {}
        raw_memories = state.get("memories", []) if isinstance(state.get("memories"), list) else []
        has_flattened_profile = any(
            ConversationMemoryStore._is_profile_memory_item(item)
            for item in raw_memories
            if isinstance(item, dict)
        )
        if has_flattened_profile or not ConversationMemoryStore._profile_items_from_any_state(state):
            source_memories = list(raw_memories)
        else:
            source_memories = []
            for item in ConversationMemoryStore._profile_items_from_any_state(state):
                if not isinstance(item, dict):
                    continue
                source_memories.append({
                    "id": str(item.get("id", "") or "").strip(),
                    "importance": "高",
                    "type": str(item.get("type", "") or item.get("category", "") or "").strip(),
                    "category": str(item.get("category", "") or item.get("type", "") or "").strip(),
                    "content": str(item.get("content", "") or "").strip(),
                    "created_at": str(item.get("created_at", "") or "").strip(),
                    "updated_at": str(item.get("updated_at", "") or "").strip(),
                })
            source_memories.extend(
                item for item in ConversationMemoryStore._memory_items_from_any_state(state)
                if isinstance(item, dict)
            )
            source_memories.sort(key=self._proposal_memory_sort_key)
        memories = []
        for item in source_memories:
            if not isinstance(item, dict):
                continue
            memories.append({
                "id": str(item.get("id", "") or "").strip(),
                "importance": str(item.get("importance", "") or "中").strip(),
                "type": str(item.get("type", "") or item.get("category", "") or "").strip(),
                "content": str(item.get("content", "") or "").strip(),
            })
        return {
            "chat_name": str(state.get("chat_name", "") or "").strip(),
            "memories": memories,
        }

    def _proposal_memory_sort_key(self, item):
        importance_order = {"高": 0, "中": 1, "低": 2}
        item = item if isinstance(item, dict) else {}
        updated_at = self._parse_time(item.get("updated_at"))
        created_at = self._parse_time(item.get("created_at"))
        updated_ts = float("inf") if updated_at is None else -updated_at.timestamp()
        created_ts = float("inf") if created_at is None else -created_at.timestamp()
        return (
            importance_order.get(str(item.get("importance", "") or "").strip(), 1),
            updated_ts,
            created_ts,
            str(item.get("id", "") or "").strip(),
        )

    def extract_proposal(self, api, state, new_messages):
        prompt = self.build_extraction_prompt(state)
        history, _skipped = build_model_visible_history(new_messages or [], assistant_limit=None)
        reply = api.chat(self.EXTRACTION_TASK_MESSAGE, prompt=prompt, history=history, stream=False)
        return self.parse_proposal_response(reply)

    def repair_proposal(self, api, bad_output):
        prompt = self.build_repair_prompt(bad_output)
        reply = api.chat(str(bad_output or ""), prompt=prompt, history=[], stream=False)
        return self.parse_proposal_response(reply)

    def validate_proposal(self, proposal, state=None):
        if not isinstance(proposal, dict):
            return False, "会话记忆提案必须是 JSON 对象"
        allowed_keys = ("add", "update", "delete")
        has_state = isinstance(state, dict)
        current_ids = set()
        if has_state:
            current_ids = {
                str(item.get("id", "") or "").strip()
                for item in self.proposal_context(state).get("memories", []) or []
                if isinstance(item, dict) and str(item.get("id", "") or "").strip()
            }
        missing_keys = [key for key in allowed_keys if key not in proposal]
        if missing_keys:
            return False, f"会话记忆提案缺少字段：{', '.join(missing_keys)}"
        extra_keys = [key for key in proposal.keys() if key not in allowed_keys]
        if extra_keys:
            return False, f"会话记忆提案只允许顶层字段：add、update、delete；收到额外字段：{', '.join(extra_keys)}"
        for key in allowed_keys:
            if not isinstance(proposal.get(key), list):
                return False, f"会话记忆提案字段 {key} 必须是数组"
        for item in proposal.get("add", []) or []:
            if not isinstance(item, dict):
                return False, "add 条目必须是对象"
            if str(item.get("importance", "") or "").strip() not in IMPORTANCE_LEVELS:
                return False, "add 条目的重要度必须是高、中、低"
            if not str(item.get("type", "") or item.get("category", "") or "").strip():
                return False, "add 条目的类型不能为空"
            if not str(item.get("content", "") or "").strip():
                return False, "add 条目的内容不能为空"
        for item in proposal.get("update", []) or []:
            if not isinstance(item, dict):
                return False, "update 条目必须是对象"
            item_id = str(item.get("id", "") or "").strip()
            if not item_id:
                return False, "update 条目必须包含 id"
            if has_state and item_id not in current_ids:
                return False, f"update 条目的 id 不存在：{item_id}"
            if str(item.get("importance", "") or "").strip() not in IMPORTANCE_LEVELS:
                return False, "update 条目的重要度必须是高、中、低"
            if not str(item.get("type", "") or item.get("category", "") or "").strip():
                return False, "update 条目的类型不能为空"
            if not str(item.get("content", "") or "").strip():
                return False, "update 条目的内容不能为空"
        for item in proposal.get("delete", []) or []:
            if isinstance(item, dict):
                item_id = str(item.get("id", "") or "").strip()
                reason = str(item.get("reason", "") or "").strip()
            else:
                item_id = str(item or "").strip()
                reason = ""
            if not item_id:
                return False, "delete 条目必须包含 id"
            if has_state and item_id not in current_ids:
                return False, f"delete 条目的 id 不存在：{item_id}"
            if not reason:
                return False, "delete 条目必须包含 reason"
        current_memory_count = len(ConversationMemoryStore._memory_items_from_any_state(state)) if has_state else 0
        if current_memory_count < 5 and (proposal.get("delete") or []):
            return False, "当前记忆条目少于 5 条时，不允许 delete"
        return True, ""

    def extract_valid_proposal(self, api, state, new_messages):
        try:
            proposal = self.extract_proposal(api, state, new_messages)
            ok, message = self.validate_proposal(proposal, state=state)
            if ok:
                return proposal, False
            bad_output = json.dumps(proposal, ensure_ascii=False, indent=2)
        except ValueError as original_error:
            message = str(original_error)
            bad_output = getattr(original_error, "bad_output", "") or ""
        try:
            repaired = self.repair_proposal(api, bad_output)
        except ValueError as repair_error:
            error = ValueError(message)
            error.bad_output = getattr(repair_error, "bad_output", "") or bad_output
            raise error
        ok, repair_message = self.validate_proposal(repaired, state=state)
        if ok:
            return repaired, True
        error = ValueError(repair_message)
        error.bad_output = json.dumps(repaired, ensure_ascii=False, indent=2) if isinstance(repaired, (dict, list)) else str(repaired or "")
        raise error

    def parse_proposal_response(self, text):
        raw = self.clean_think_tags(text)
        match = MD_FENCE_RE.search(raw)
        cleaned = (match.group(1).strip() if match else raw).strip()
        try:
            return json.loads(cleaned)
        except Exception as exc:
            error = ValueError(f"会话记忆提案 JSON 无法解析：{exc}")
            error.bad_output = cleaned
            raise error

    def clean_think_tags(self, text):
        return sanitize_ai_output_text(text)

    def select_new_messages(self, state, messages, protected_count=None):
        selected = self._messages_after_checkpoint(state, messages if isinstance(messages, list) else [], protected_count=protected_count)
        return selected[:self.analysis_limit]

    def _messages_after_checkpoint(self, state, messages, protected_count=None):
        messages = messages if isinstance(messages, list) else []
        if protected_count is None:
            protected_count = self.protected_recent_count
        protected_count = max(0, self._coerce_int(protected_count, 0))
        candidates = messages[:-protected_count] if protected_count and len(messages) > protected_count else ([] if protected_count else messages)
        maintenance = state.get("maintenance", {}) if isinstance(state, dict) else {}
        last_key = str(maintenance.get("last_processed_message_key", "") or "").strip()
        if last_key:
            for idx in range(len(candidates) - 1, -1, -1):
                if self.message_key(candidates[idx]) == last_key:
                    return candidates[idx + 1:]
        last_time = self._parse_time(maintenance.get("last_processed_message_time"))
        if last_time is not None:
            selected = []
            for item in candidates:
                item_at = self._parse_time(item.get("time")) if isinstance(item, dict) else None
                if item_at and item_at > last_time:
                    selected.append(item)
            return selected
        return candidates

    def processed_cursor(self, processed_messages):
        if not processed_messages:
            return {"last_processed_message_key": "", "last_processed_message_time": ""}
        last = processed_messages[-1]
        return {
            "last_processed_message_key": self.message_key(last),
            "last_processed_message_time": str(last.get("time", "") or "") if isinstance(last, dict) else "",
        }

    def message_key(self, message):
        if not isinstance(message, dict):
            return ""
        content = str(message.get("content", "") or "")
        digest = hashlib.sha1(content.encode("utf-8", errors="ignore")).hexdigest()[:8]
        return "|".join([
            str(message.get("time", "") or ""),
            str(message.get("sender", "") or ""),
            str(message.get("attr", "") or ""),
            str(message.get("type", "") or ""),
            digest,
        ])

    def format_messages(self, messages):
        lines = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            time = str(item.get("time", "") or "").strip()
            sender = str(item.get("sender", "") or item.get("attr", "") or "").strip()
            content = str(item.get("content", "") or "").strip()
            if not content:
                continue
            prefix = f"[{time}] " if time else ""
            if sender:
                prefix += f"{sender}: "
            lines.append(prefix + content)
        return "\n".join(lines)

    def _coerce_int(self, value, default):
        try:
            return int(value)
        except Exception:
            return default

    def _parse_time(self, value):
        if isinstance(value, datetime):
            return value
        value = str(value or "").strip()
        if not value:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
            try:
                return datetime.strptime(value, fmt)
            except Exception:
                pass
        return None

    def _latest_time(self, *values):
        parsed = [self._parse_time(value) for value in values]
        parsed = [value for value in parsed if value is not None]
        return max(parsed) if parsed else None


class PromptSystem:
    """Route private chats to prompt templates and build dynamic prompts."""

    def __init__(self, config, state_dir, state_store=None, prompt_builder=None, memory_extractor=None, prompt_dir=None, chat_name_resolver=None):
        self.config = config or {}
        self.state_store = state_store or ConversationMemoryStore(state_dir)
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.memory_extractor = memory_extractor or self._build_memory_extractor()
        self.prompt_dir = prompt_dir or os.path.join(app_base_dir(), "data", "prompt")
        self.chat_name_resolver = chat_name_resolver

    def _get(self, key, default=None):
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return getattr(self.config, key, default)

    def resolve_chat_name(self, chat_name):
        resolver = getattr(self, "chat_name_resolver", None)
        if callable(resolver):
            try:
                resolved = str(resolver(chat_name) or "").strip()
                if resolved:
                    return resolved
            except Exception:
                pass
        return str(chat_name or "").strip()

    def _build_memory_extractor(self):
        return ConversationMemoryExtractor(
            message_threshold=self._coerce_config_int(
                self._get("conversation_memory_message_threshold", 100), 100, 10, 200
            ),
            interval_hours=self._coerce_config_int(
                self._get("conversation_memory_interval_hours", 12), 12, 1, 72
            ),
            protected_recent_count=self._coerce_config_int(
                self._get("conversation_memory_protected_recent_count", CONVERSATION_MEMORY_DEFAULT_PROTECTED_RECENT_COUNT),
                CONVERSATION_MEMORY_DEFAULT_PROTECTED_RECENT_COUNT,
                0,
                200,
            ),
        )

    @staticmethod
    def _coerce_config_int(value, default, min_value, max_value):
        try:
            value = int(value)
        except Exception:
            value = default
        return max(min_value, min(max_value, value))

    def prompt_name_for(self, chat_name, chat_type="private"):
        if chat_type == "group":
            prompt_map = self._get("group_prompt_map", {}) or {}
            if isinstance(prompt_map, dict):
                override = str(prompt_map.get(chat_name, "") or "").strip()
                if override:
                    return override
            return str(self._get("default_prompt", "") or "").strip()
        prompt_map = self._get("chat_prompt_map", {}) or {}
        listen_list = self._get("listen_list", []) or []
        is_whitelist_user = isinstance(listen_list, list) and chat_name in listen_list
        if is_whitelist_user and isinstance(prompt_map, dict):
            override = str(prompt_map.get(chat_name, "") or "").strip()
            if override:
                return override
        return str(self._get("default_prompt", "") or "").strip()

    def base_prompt_for(self, chat_name, chat_type="private"):
        prompt_name = self.prompt_name_for(chat_name, chat_type=chat_type)
        getter = getattr(self.config, "get_prompt_content", None)
        if callable(getter):
            try:
                return str(getter(prompt_name) or "").strip()
            except Exception:
                pass
        if not prompt_name:
            return ""
        path = os.path.join(self.prompt_dir, f"{prompt_name}.md")
        try:
            with open(path, "r", encoding="utf-8") as f:
                return str(f.read() or "").strip()
        except Exception:
            return ""

    def context_values_for(self, chat_name, *, chat_type="private", now=None, base_prompt=None):
        display_chat_name = str(chat_name or "").strip()
        storage_chat_name = self.resolve_chat_name(display_chat_name)
        prompt_name = self.prompt_name_for(display_chat_name, chat_type=chat_type)
        resolved_base_prompt = str(
            base_prompt if base_prompt is not None else self.base_prompt_for(display_chat_name, chat_type=chat_type)
        ).strip()
        if not resolved_base_prompt:
            resolved_base_prompt = "（未提供）"

        persona_status_block = self.load_persona_status(prompt_name) if prompt_name else ""
        conversation_memory_section = ""
        if self.enabled_for(display_chat_name, chat_type=chat_type):
            if self.memory_enabled_for(display_chat_name, chat_type=chat_type):
                state = self.state_store.load_state(storage_chat_name, prompt_name, strict=True)
            else:
                state = self.state_store.default_state(display_chat_name, prompt_name)
            conversation_memory_section = str(
                self.prompt_builder.build_conversation_memory_section(state) or ""
            ).strip()

        return {
            "base_prompt": resolved_base_prompt,
            "persona_status_block": str(persona_status_block or "").strip(),
            "conversation_memory_section": conversation_memory_section,
            "now": now or datetime.now().strftime("%Y-%m-%d %H:%M"),
            "chat_name": display_chat_name,
        }

    def render_template_prompt(
        self,
        filename,
        chat_name,
        values=None,
        *,
        chat_type="private",
        now=None,
        base_prompt=None,
        required_placeholders=(),
    ):
        render_values = self.context_values_for(
            chat_name,
            chat_type=chat_type,
            now=now,
            base_prompt=base_prompt,
        )
        render_values.update(dict(values or {}))
        required = (
            "{{base_prompt}}",
            "{{persona_status_block}}",
            "{{conversation_memory_section}}",
            *tuple(required_placeholders or ()),
        )
        return self.prompt_builder.prompt_store.render(
            filename,
            render_values,
            required_placeholders=tuple(dict.fromkeys(required)),
        ).strip()

    def render_moments_caption_prompt(self, chat_name, raw_text, *, chat_type="private", now=None, base_prompt=None):
        text = str(raw_text or "").strip() or "（未提供）"
        context_values = self.context_values_for(
            chat_name,
            chat_type=chat_type,
            now=now,
            base_prompt=base_prompt,
        )
        return self.prompt_builder.prompt_store.render(
            "moments_caption.md",
            {
                "base_prompt": str(context_values.get("base_prompt") or "").strip() or "（未提供）",
                "persona_status_block": str(context_values.get("persona_status_block") or "").strip() or "（未提供）",
                "raw_text_block": text,
            },
            required_placeholders=(
                "{{base_prompt}}",
                "{{persona_status_block}}",
                "{{raw_text_block}}",
            ),
        )

    def enabled_for(self, chat_name, chat_type="private"):
        if chat_type == "group":
            return False
        return bool(self.prompt_name_for(chat_name))

    def memory_enabled_for(self, chat_name, chat_type="private"):
        if not self.enabled_for(chat_name, chat_type=chat_type):
            return False
        if not bool(self._get("conversation_memory_switch", True)):
            return False
        excluded = self._get("conversation_memory_exclude_list", []) or []
        return str(chat_name) not in {str(item) for item in excluded}

    def build_prompt(
        self,
        chat_name,
        history,
        message,
        *,
        base_prompt=None,
        chat_type="private",
        now=None,
        image_parse_block="",
        prompt_extra="",
    ):
        resolved_base_prompt = str(
            base_prompt if base_prompt is not None else self.base_prompt_for(chat_name, chat_type=chat_type)
        ).strip()
        extra_block = str(prompt_extra or "").strip()
        if extra_block:
            resolved_base_prompt = "\n\n".join(part for part in (resolved_base_prompt, extra_block) if part)
        display_chat_name = str(chat_name or "").strip()
        storage_chat_name = self.resolve_chat_name(display_chat_name)
        if not self.enabled_for(display_chat_name, chat_type=chat_type):
            return resolved_base_prompt
        prompt_name = self.prompt_name_for(display_chat_name, chat_type=chat_type)
        if self.memory_enabled_for(display_chat_name, chat_type=chat_type):
            state = self.state_store.load_state(storage_chat_name, prompt_name, strict=True)
        else:
            state = self.state_store.default_state(display_chat_name, prompt_name)
        persona_status_block = self.load_persona_status(prompt_name)
        return self.prompt_builder.build(
            display_chat_name,
            resolved_base_prompt,
            state,
            current_message=message,
            now=now,
            image_parse_block=image_parse_block,
            persona_status_block=persona_status_block,
        )

    def load_persona_status(self, prompt_name):
        prompt_name = str(prompt_name or "").strip()
        if not prompt_name:
            return ""
        path = os.path.join(self.prompt_dir, f"{prompt_name}{PERSONA_STATUS_SUFFIX}.md")
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read().strip()
        except Exception:
            return ""
        if not text:
            return ""
        return text

    def auto_memory_enabled_for(self, chat_name, chat_type="private"):
        return self.memory_enabled_for(chat_name, chat_type=chat_type)

    def update_memory(self, chat_name, messages, api, chat_type="private", protected_count=None, now=None):
        display_chat_name = str(chat_name or "").strip()
        storage_chat_name = self.resolve_chat_name(display_chat_name)
        if not self.auto_memory_enabled_for(display_chat_name, chat_type=chat_type):
            return None
        messages = messages if isinstance(messages, list) else []
        prompt_name = self.prompt_name_for(display_chat_name)
        try:
            state = self.state_store.load_state(storage_chat_name, prompt_name, strict=True)
        except ValueError:
            return None
        if not self.memory_extractor.should_extract(state, messages, protected_count=protected_count, now=now):
            return None
        new_messages = self.memory_extractor.select_new_messages(state, messages, protected_count=protected_count)
        if not new_messages:
            return None
        try:
            proposal, _ = self.memory_extractor.extract_valid_proposal(api, state, new_messages)
        except ValueError:
            state.setdefault("maintenance", {})
            state["maintenance"]["last_attempted_at"] = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                self.state_store.save_state(storage_chat_name, state)
            except ValueError:
                pass
            return None
        merged = self.state_store.merge_proposal(
            state,
            proposal,
            chat_name=storage_chat_name,
            wx_id=state.get("wx_id", "") if isinstance(state, dict) else "",
            now=now or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        ok, _ = self.state_store.validate_state(merged)
        if not ok:
            state.setdefault("maintenance", {})
            state["maintenance"]["last_attempted_at"] = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                self.state_store.save_state(storage_chat_name, state)
            except ValueError:
                pass
            return None
        cursor = self.memory_extractor.processed_cursor(new_messages)
        cursor["last_processed_at"] = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor["last_attempted_at"] = cursor["last_processed_at"]
        try:
            merged.setdefault("maintenance", {})
            merged["maintenance"].update(cursor)
            return self.state_store.save_state(storage_chat_name, merged)
        except ValueError:
            state.setdefault("maintenance", {})
            state["maintenance"]["last_attempted_at"] = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                self.state_store.save_state(storage_chat_name, state)
            except ValueError:
                pass
            return None
