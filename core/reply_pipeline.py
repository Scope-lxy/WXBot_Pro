"""Reply strategy helpers shared by private and group chats."""

from dataclasses import dataclass
from typing import Any, Callable, Optional

from core.vision_bridge import VisionNote


@dataclass
class ImageReplyRequest:
    chat_name: str
    chat_type: str
    attached_text: str
    sender: str
    history: list
    final_api: Any
    recognition_api: Any
    final_api_supports_vision: bool
    image_path: str = ""
    image_paths: Optional[list[str]] = None
    image_senders: Optional[list[str]] = None
    visual_notes: Optional[list[str]] = None
    on_visual_notes: Optional[Callable[[list[str], list[str]], None]] = None
    final_api_index: int = 0
    recognition_api_index: int = 0
    message_time: str = ""


class ImageReplyPipeline:
    """Choose direct-vision or two-stage image replies without sending messages."""

    def __init__(
        self,
        *,
        prompt_builder: Callable[..., str],
        image_parse_block_builder: Callable[..., str],
        user_message_builder: Callable[..., str],
        vision_bridge: Any,
        log_info: Optional[Callable[[str], None]] = None,
    ):
        self.prompt_builder = prompt_builder
        self.image_parse_block_builder = image_parse_block_builder
        self.user_message_builder = user_message_builder
        self.vision_bridge = vision_bridge
        self.log_info = log_info or (lambda message: None)

    def reply(self, request: ImageReplyRequest):
        if request.final_api_supports_vision:
            return self._reply_with_direct_vision(request)
        return self._reply_with_two_stage_parse(request)

    @staticmethod
    def _resolved_image_paths(request: ImageReplyRequest):
        paths = [str(path or "").strip() for path in (request.image_paths or []) if str(path or "").strip()]
        if paths:
            return paths
        path = str(request.image_path or "").strip()
        return [path] if path else []

    @staticmethod
    def _normalize_visual_notes(request: ImageReplyRequest, image_paths):
        notes = list(request.visual_notes or [])
        normalized = []
        for index, image_path in enumerate(image_paths):
            if index < len(notes) and str(notes[index] or "").strip():
                normalized.append(VisionNote.from_recognition_text(notes[index]))
            else:
                normalized.append(None)
        return normalized

    @staticmethod
    def _normalize_image_senders(request: ImageReplyRequest, image_paths):
        senders = list(request.image_senders or [])
        return [
            str(senders[index] or "").strip() if index < len(senders) else ""
            for index, _path in enumerate(image_paths)
        ]

    def _reply_with_direct_vision(self, request: ImageReplyRequest):
        scene = "群聊" if request.chat_type == "group" else "私聊"
        self.log_info(
            f"{scene} {request.chat_name}：AI 正在识别图片并生成回复"
        )
        image_paths = self._resolved_image_paths(request)
        image_senders = self._normalize_image_senders(request, image_paths)
        message = self.user_message_builder(
            request.chat_type,
            sender=request.sender,
            attached_text=request.attached_text,
            image_count=len(image_paths),
            image_senders=image_senders,
            message_time=request.message_time,
        )
        prompt = self.prompt_builder(
            request.chat_name,
            chat_type=request.chat_type,
            image_parse_block=self.image_parse_block_builder(),
        )
        kwargs = {
            "prompt": prompt,
            "history": request.history,
        }
        if len(image_paths) > 1:
            kwargs["image_paths"] = image_paths
        elif image_paths:
            kwargs["image_path"] = image_paths[0]
        return request.final_api.chat(
            message,
            **kwargs,
        )

    def _reply_with_two_stage_parse(self, request: ImageReplyRequest):
        scene = "群聊" if request.chat_type == "group" else "私聊"
        self.log_info(
            f"{scene} {request.chat_name}：AI 正在先识别图片内容，再生成回复"
        )
        image_paths = self._resolved_image_paths(request)
        image_senders = self._normalize_image_senders(request, image_paths)
        normalized_notes = self._normalize_visual_notes(request, image_paths)
        notes = []
        for index, image_path in enumerate(image_paths):
            note = normalized_notes[index]
            if note is None:
                note = self.vision_bridge.analyze(
                    image_path=image_path,
                    recognition_api=request.recognition_api,
                    chat_type=request.chat_type,
                    sender=image_senders[index] or request.sender,
                    # Visual notes are saved as image summaries, so keep them image-only.
                    attached_text="",
                )
            notes.append(note)
        if callable(request.on_visual_notes):
            request.on_visual_notes(image_paths, [note.render() for note in notes])
        final_message = self.user_message_builder(
            request.chat_type,
            sender=request.sender,
            attached_text=request.attached_text,
            image_count=len(image_paths),
            image_senders=image_senders,
            visual_notes=[note.render() for note in notes],
            message_time=request.message_time,
        )
        image_parse_block = self.image_parse_block_builder()

        prompt = self.prompt_builder(
            request.chat_name,
            chat_type=request.chat_type,
            image_parse_block=image_parse_block,
        )
        return request.final_api.chat(final_message, prompt=prompt, history=request.history)
