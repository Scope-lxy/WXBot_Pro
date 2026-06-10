"""Structured auxiliary-vision bridge for text-only reply models."""

from dataclasses import dataclass
import re
from typing import Any, Callable, Dict

from core.media import image_content_hash
from core.sending import sanitize_ai_output_text

_SECTION_LABELS = {
    "图片概览": "overview",
    "可见文字": "visible_text",
    "关键细节": "key_details",
    "不确定项": "uncertainty",
}
_SECTION_LINE_RE = re.compile(r"^(?:[-*•]\s*)?(图片概览|可见文字|关键细节|不确定项)\s*[:：]\s*(.*)$")


def _clean_text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _truncate_line(value: str, limit: int = 80) -> str:
    text = _clean_text(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


@dataclass(frozen=True)
class VisionNote:
    overview: str
    visible_text: str
    key_details: str
    uncertainty: str
    recognition_failed: bool = False

    def render(self) -> str:
        return "\n".join([
            f"图片概览：{self.overview}",
            f"可见文字：{self.visible_text}",
            f"关键细节：{self.key_details}",
            f"不确定项：{self.uncertainty}",
        ])

    @classmethod
    def fallback(cls) -> "VisionNote":
        return cls(
            overview="暂时无法可靠判断。",
            visible_text="未提取到明确文字。",
            key_details="只确认这是一张图片，其余细节暂时不稳定。",
            uncertainty="图片有些细节暂时看不清。",
            recognition_failed=True,
        )

    @classmethod
    def from_recognition_text(cls, text: Any) -> "VisionNote":
        raw = _clean_text(sanitize_ai_output_text(text))
        if not raw:
            return cls.fallback()

        sections: Dict[str, str] = {}
        current_key = ""
        for line in raw.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            matched = _SECTION_LINE_RE.match(stripped)
            if matched:
                label, value = matched.groups()
                current_key = _SECTION_LABELS[label]
                sections[current_key] = value.strip()
                continue
            if current_key:
                extra = stripped
                sections[current_key] = f"{sections[current_key]}\n{extra}".strip()

        if sections:
            return cls(
                overview=sections.get("overview") or "暂时无法可靠判断。",
                visible_text=sections.get("visible_text") or "未提取到明确文字。",
                key_details=sections.get("key_details") or "未提取到稳定细节。",
                uncertainty=sections.get("uncertainty") or "暂无额外不确定项说明。",
            )

        summary = _truncate_line(raw)
        return cls(
            overview=summary or "暂时无法可靠判断。",
            visible_text="未提取到明确文字。",
            key_details=raw,
            uncertainty="辅助视觉结果未按结构输出，细节可靠性一般。",
        )


class VisionBridge:
    """Call the recognition model once and normalize it into a stable note."""

    def __init__(
        self,
        *,
        description_prompt_builder: Callable[..., str],
        description_system_prompt: str,
        image_hash_builder: Callable[[str], str] = image_content_hash,
        log_warning: Callable[[str], None] | None = None,
    ):
        self.description_prompt_builder = description_prompt_builder
        self.description_system_prompt = description_system_prompt
        self.image_hash_builder = image_hash_builder
        self.log_warning = log_warning or (lambda _message: None)
        self._note_cache: Dict[str, VisionNote] = {}

    def analyze(
        self,
        *,
        image_path: str,
        recognition_api: Any,
        chat_type: str = "private",
        sender: str = "",
        attached_text: str = "",
    ) -> VisionNote:
        cache_key = self._cache_key(
            image_path=image_path,
            chat_type=chat_type,
            sender=sender,
            attached_text=attached_text,
            prompt_text=self.description_prompt_builder(
                chat_type=chat_type,
                sender=sender,
                attached_text=attached_text,
            ),
            recognition_api=recognition_api,
        )
        if cache_key and cache_key in self._note_cache:
            return self._note_cache[cache_key]

        try:
            raw_text = recognition_api.chat(
                self.description_prompt_builder(
                    chat_type=chat_type,
                    sender=sender,
                    attached_text=attached_text,
                ),
                prompt=self.description_system_prompt,
                history=[],
                image_path=image_path,
            )
        except Exception as exc:
            self.log_warning(f"图片识别失败，改用结构化视觉兜底：{exc}")
            return VisionNote.fallback()

        note = VisionNote.from_recognition_text(raw_text)
        if cache_key:
            self._note_cache[cache_key] = note
        return note

    def _cache_key(
        self,
        *,
        image_path: str,
        chat_type: str,
        sender: str,
        attached_text: str,
        prompt_text: str,
        recognition_api: Any,
    ) -> str:
        image_digest = _clean_text(self.image_hash_builder(image_path))
        if not image_digest:
            return ""
        return "|".join([
            image_digest,
            _clean_text(chat_type),
            _clean_text(sender),
            _clean_text(attached_text),
            _clean_text(prompt_text),
            self._recognition_signature(recognition_api),
            _clean_text(self.description_system_prompt),
        ])

    @staticmethod
    def _recognition_signature(recognition_api: Any) -> str:
        if recognition_api is None:
            return "none"
        model = _clean_text(getattr(recognition_api, "DS_NOW_MOD", ""))
        if not model:
            config = getattr(recognition_api, "config", None)
            model = _clean_text(getattr(config, "model", ""))
        return f"{recognition_api.__class__.__name__}:{model}"
