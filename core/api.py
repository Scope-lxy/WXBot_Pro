"""Shared API config helpers and model-provider adapters."""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import time
from dataclasses import dataclass

import requests
from openai import OpenAI

from core.chat_history_format import format_history_message
from core.logger import log
from core.media import prepare_ai_image_path
from core.tts import (
    default_tts_model,
    default_tts_sdk,
    get_tts_model_meta,
    get_tts_sdk_meta,
    list_tts_model_options,
    list_tts_sdk_options,
    resolve_tts_model,
    resolve_tts_sdk,
)


_CHAT_API_APP_VERSION = ""
DEFAULT_CHAT_MAX_OUTPUT_TOKENS = 25600
MAIN_API_REQUEST_TIMEOUT_SECONDS = 60
DEFAULT_REASONING_EFFORT = "medium"
REASONING_EFFORT_VALUES = ("none", "minimal", "low", "medium", "high", "xhigh")
DEFAULT_API_PROTOCOL = "chat_completions"
API_PROTOCOL_VALUES = ("responses", "chat_completions")
API_ERROR_REPLY_TEXT = "API返回错误，请稍后再试"


def is_api_error_reply(value) -> bool:
    return str(value or "").strip() == API_ERROR_REPLY_TEXT


def set_chat_api_app_version(version_text):
    global _CHAT_API_APP_VERSION
    _CHAT_API_APP_VERSION = str(version_text or "").strip()


def _chat_api_user_agent():
    if _CHAT_API_APP_VERSION:
        return f"siver-wxbot-panel/{_CHAT_API_APP_VERSION}"
    return "siver-wxbot-panel"


def normalize_reasoning_effort(value):
    effort = str(value or "").strip().lower()
    if effort in REASONING_EFFORT_VALUES:
        return effort
    return DEFAULT_REASONING_EFFORT


def normalize_api_protocol(value):
    protocol = str(value or "").strip().lower()
    if protocol in API_PROTOCOL_VALUES:
        return protocol
    return DEFAULT_API_PROTOCOL


def _truncate_log_text(value, limit=300):
    text = str(value or "").replace("\r", "\\r").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[:limit] + f"...（已截断，原始长度 {len(text)}）"


def _api_error_status_code(error):
    for candidate in (
        getattr(error, "status_code", None),
        getattr(getattr(error, "response", None), "status_code", None),
    ):
        try:
            if candidate is not None:
                return int(candidate)
        except (TypeError, ValueError):
            pass
    match = re.search(r"(?:error code|status(?: code)?)\s*[:=]\s*(\d{3})", str(error or ""), re.I)
    return int(match.group(1)) if match else None


def _api_error_provider_message(error):
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        payload = body.get("error", body)
        if isinstance(payload, dict) and payload.get("message"):
            return str(payload["message"])
        if body.get("message"):
            return str(body["message"])
    message = getattr(error, "message", None)
    return str(message or error or "").strip()


def describe_api_error(error):
    """Return a concise, actionable Chinese summary for panel-facing API logs."""
    status_code = _api_error_status_code(error)
    error_type = type(error).__name__.lower() if not isinstance(error, str) else ""
    detail = _api_error_provider_message(error)
    lower = detail.lower()

    def with_code(summary):
        if status_code is None:
            return summary
        return f"{summary}（错误码 {status_code}）"

    if "image_url" in lower and any(marker in lower for marker in ("expected `text`", "expected text", "unknown variant", "not support")):
        return with_code("接口不支持图片输入")
    if status_code == 402 or any(marker in lower for marker in ("insufficient balance", "insufficient quota", "余额不足")):
        return with_code("余额或套餐额度不足")
    if any(marker in lower for marker in ("context length", "maximum context", "too many tokens", "上下文长度")):
        return with_code("内容超过模型上下文限制")
    if any(marker in lower for marker in ("model_not_found", "model not found", "model does not exist", "unknown model")):
        return with_code("模型不存在或无权使用")
    if status_code == 401 or any(marker in lower for marker in ("invalid api key", "incorrect api key", "authentication")):
        return with_code("API Key 无效")
    if status_code == 403:
        return with_code("账号无调用权限")
    if status_code == 404:
        return with_code("接口地址或模型不存在")
    if status_code == 429 or "rate limit" in lower:
        return with_code("请求频繁或额度受限")
    if status_code == 400:
        return with_code("请求参数不被接口接受")
    if status_code in (408, 504) or "timeout" in error_type or any(marker in lower for marker in ("timed out", "timeout", "请求超时")):
        return with_code("接口响应超时")
    if "connection" in error_type or any(marker in lower for marker in ("connection error", "connection refused", "connection reset")):
        return with_code("无法连接接口")
    if status_code is not None and status_code >= 500:
        return with_code("服务商暂时异常")
    if isinstance(error, ValueError) and any(marker in lower for marker in ("响应为空", "没有 choices", "未找到文本内容")):
        return with_code("接口未返回有效文本")
    return with_code("接口请求失败")


def format_api_error_log_message(api_label, error, *, action=""):
    summary = describe_api_error(error)
    if action:
        summary = f"{summary}；{str(action).strip()}"
    technical_detail = _truncate_log_text(f"{type(error).__name__}: {error}", 1200)
    return f"API调用失败（{api_label}）：{summary}\n技术详情：{technical_detail}"


def _to_debug_jsonable(value):
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(key): _to_debug_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_debug_jsonable(item) for item in value]
    return value


def _format_api_debug_payload(value, limit=12000):
    try:
        text = json.dumps(_to_debug_jsonable(value), ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return _truncate_log_text(text, limit)


def _get_response_field(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _summarize_response_data(data):
    if not isinstance(data, dict):
        return _truncate_log_text(data)
    summary = {
        "keys": sorted(str(key) for key in data.keys())[:12],
    }
    for key in ("id", "object", "status", "model"):
        value = data.get(key)
        if value is not None:
            summary[key] = _truncate_log_text(value, 120)
    error = data.get("error")
    if isinstance(error, dict):
        summary["error"] = {
            "type": _truncate_log_text(error.get("type"), 120),
            "code": _truncate_log_text(error.get("code"), 120),
            "message": _truncate_log_text(error.get("message"), 300),
        }
    elif error:
        summary["error"] = _truncate_log_text(error, 300)
    output_text = data.get("output_text")
    if isinstance(output_text, str):
        summary["output_text_len"] = len(output_text)
        summary["output_text_preview"] = _truncate_log_text(output_text, 120)
    output = data.get("output")
    if isinstance(output, list):
        summary["output_count"] = len(output)
        summary["output_types"] = [
            str(item.get("type", "")) if isinstance(item, dict) else type(item).__name__
            for item in output[:8]
        ]
    content = data.get("content")
    if isinstance(content, list):
        summary["content_count"] = len(content)
        summary["content_types"] = [
            str(item.get("type", "")) if isinstance(item, dict) else type(item).__name__
            for item in content[:8]
        ]
    return json.dumps(summary, ensure_ascii=False, separators=(",", ":"))


def _summarize_request_payload(payload):
    if not isinstance(payload, dict):
        return _truncate_log_text(payload)
    summary = {}
    for key in ("model", "max_tokens", "max_output_tokens", "stream", "reasoning"):
        if key in payload:
            summary[key] = payload.get(key)
    items = payload.get("input") or payload.get("messages")
    if isinstance(items, list):
        summary["item_count"] = len(items)
        summary["roles"] = [
            str(item.get("role", "")) if isinstance(item, dict) else type(item).__name__
            for item in items[:12]
        ]
        has_image = False
        for item in items:
            content = item.get("content") if isinstance(item, dict) else None
            blocks = content if isinstance(content, list) else []
            if any(isinstance(block, dict) and block.get("type") in ("image", "input_image", "image_url") for block in blocks):
                has_image = True
                break
        summary["has_image"] = has_image
    return json.dumps(summary, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class APIConfigSnapshot:
    sdk: str = ""
    key: str = ""
    url: str = ""
    model: str = ""
    prompt: str = ""
    max_retries: int = 5
    max_output_tokens: int = DEFAULT_CHAT_MAX_OUTPUT_TOKENS
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    api_protocol: str = DEFAULT_API_PROTOCOL


def build_api_config_snapshot(config=None, *, prompt="", max_retries=5, max_output_tokens=DEFAULT_CHAT_MAX_OUTPUT_TOKENS):
    config = config if isinstance(config, dict) else {}
    if max_retries is None:
        max_retries = 5
    return APIConfigSnapshot(
        sdk=str(config.get("sdk", "") or "").strip(),
        key=str(config.get("key", "") or "").strip(),
        url=str(config.get("url", "") or "").strip().rstrip("/"),
        model=str(config.get("model", "") or "").strip(),
        prompt=str(prompt or ""),
        max_retries=max(0, int(max_retries)),
        max_output_tokens=max(1, int(max_output_tokens or DEFAULT_CHAT_MAX_OUTPUT_TOKENS)),
        reasoning_effort=normalize_reasoning_effort(config.get("reasoning_effort")),
        api_protocol=normalize_api_protocol(config.get("api_protocol")),
    )


def format_api_display_name(api_configs, index, *, fallback="未连接"):
    configs = api_configs if isinstance(api_configs, list) else []
    try:
        idx = int(index)
    except (TypeError, ValueError):
        return fallback
    if idx < 0 or idx >= len(configs):
        return fallback
    item = configs[idx] if isinstance(configs[idx], dict) else {}
    model = str(item.get("model", "") or "").strip()
    sdk = str(item.get("sdk", "") or "").strip()
    if model:
        return f"接口 {idx + 1}（{model}）"
    if sdk:
        return f"接口 {idx + 1}（{sdk}）"
    return f"接口 {idx + 1}"


def _api_log_label(config, _api_name="", *, model=None):
    name = (
        str(model or "").strip()
        or str(getattr(config, "model", "") or "").strip()
        or str(getattr(config, "sdk", "") or "").strip()
        or "未知接口"
    )
    return f"接口：{name}"


class OpenAIAPI:
    def __init__(self, config):
        self.config = config
        self.DS_NOW_MOD = config.model
        self.last_protocol_status = {"status": "unknown"}
        self.last_error = None
        self.client = OpenAI(
            api_key=config.key,
            base_url=config.url,
            timeout=float(MAIN_API_REQUEST_TIMEOUT_SECONDS),
            max_retries=int(getattr(config, "max_retries", 5)),
            default_headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "*/*",
            },
        )

    def _log_label(self, api_name="", *, model=None):
        return _api_log_label(self.config, api_name, model=model)

    @staticmethod
    def _image_to_data_url(image_path: str = "", image_url: str = "") -> str:
        if image_path:
            image_path = prepare_ai_image_path(image_path)
            mime_type, _ = mimetypes.guess_type(image_path)
            if mime_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                mime_type = "image/jpeg"
            with open(image_path, "rb") as f:
                image_data = base64.standard_b64encode(f.read()).decode("utf-8")
            return f"data:{mime_type};base64,{image_data}"
        if image_url:
            return image_url
        raise ValueError("image_path 和 image_url 不能同时为空")

    @classmethod
    def _build_chat_image_block(cls, image_path: str = "", image_url: str = "") -> dict:
        return {
            "type": "image_url",
            "image_url": {"url": cls._image_to_data_url(image_path, image_url)},
        }

    @classmethod
    def _build_responses_image_block(cls, image_path: str = "", image_url: str = "") -> dict:
        return {
            "type": "input_image",
            "image_url": cls._image_to_data_url(image_path, image_url),
        }

    @staticmethod
    def _normalize_image_paths(image_path: str = "", image_paths=None):
        normalized = [str(path or "").strip() for path in (image_paths or []) if str(path or "").strip()]
        if normalized:
            return normalized
        single = str(image_path or "").strip()
        return [single] if single else []

    def chat(
        self,
        message,
        model=None,
        stream=False,
        prompt=None,
        history=None,
        image_path: str = "",
        image_url: str = "",
        image_paths=None,
        log_errors=True,
    ):
        if model is None:
            model = self.DS_NOW_MOD
        if prompt is None:
            prompt = self.config.prompt

        protocol = normalize_api_protocol(getattr(self.config, "api_protocol", ""))
        self.last_protocol_status = {"status": "unknown"}
        self.last_error = None
        if protocol == "responses":
            try:
                result = self._call_responses_api(message, model, stream, prompt, history, image_path, image_url, image_paths)
                self.last_protocol_status = {"status": "responses_ok"}
                return result
            except Exception as e:
                self.last_error = e
                if log_errors:
                    log(
                        level="WARNING",
                        message=format_api_error_log_message(self._log_label('Responses API', model=model), e),
                    )
                self.last_protocol_status = {"status": "failed"}
                return API_ERROR_REPLY_TEXT

        try:
            result = self._call_chat_completions_api(message, model, stream, prompt, history, image_path, image_url, image_paths)
            self.last_protocol_status = {"status": "chat_completions_ok"}
            return result
        except Exception as e:
            self.last_error = e
            if log_errors:
                log(
                    level="WARNING",
                    message=format_api_error_log_message(self._log_label('Chat Completions', model=model), e),
                )
            self.last_protocol_status = {"status": "failed"}
            return API_ERROR_REPLY_TEXT

    def _call_chat_completions_api(self, message, model, stream, prompt, history=None, image_path="", image_url="", image_paths=None):
        messages = [{"role": "system", "content": prompt}]
        if history:
            for h in history:
                messages.append(format_history_message(h))
        normalized_paths = self._normalize_image_paths(image_path, image_paths)
        if normalized_paths or image_url:
            user_content = [
                {"type": "text", "text": message},
            ]
            if normalized_paths:
                user_content.extend(self._build_chat_image_block(path, "") for path in normalized_paths)
            else:
                user_content.append(self._build_chat_image_block("", image_url))
        else:
            user_content = message
        messages.append({"role": "user", "content": user_content})

        response = self.client.chat.completions.create(
            model=model,
            messages=messages,
            stream=stream,
        )
        if stream:
            reasoning_content = ""
            content = ""
            chunk_count = 0
            for chunk in response:
                chunk_count += 1
                choices = _get_response_field(chunk, "choices")
                if not choices:
                    continue
                choice = choices[0]
                delta = _get_response_field(choice, "delta")
                if not delta:
                    continue
                reasoning_delta = _get_response_field(delta, "reasoning_content", "")
                content_delta = _get_response_field(delta, "content", "")
                if reasoning_delta:
                    reasoning_content += reasoning_delta
                if content_delta:
                    content += content_delta
            result = content.strip() if content.strip() else reasoning_content.strip()
            if result:
                return result
            raise ValueError(f"Chat Completions 流式响应为空（收到 {chunk_count} 个块）")

        choices = _get_response_field(response, "choices")
        if choices and len(choices) > 0:
            message_obj = _get_response_field(choices[0], "message")
            content = _get_response_field(message_obj, "content", "")
            reasoning_content = _get_response_field(message_obj, "reasoning_content", "")
            if content:
                output = content
                return output
            if reasoning_content:
                output = reasoning_content
                return output
            log(
                level="DEBUG",
                message=(
                    f"API空响应诊断（{self._log_label('Chat Completions', model=model)}，非流式）："
                    f"request={_format_api_debug_payload({'model': model, 'messages': messages})}；"
                    f"response={_format_api_debug_payload(response)}"
                ),
            )
            raise ValueError("Chat Completions 非流式响应内容为空")

        log(
            level="DEBUG",
            message=(
                f"API空响应诊断（{self._log_label('Chat Completions', model=model)}，非流式）："
                f"request={_format_api_debug_payload({'model': model, 'messages': messages})}；"
                f"response={_format_api_debug_payload(response)}"
            ),
        )
        raise ValueError("Chat Completions 响应中没有 choices")

    def _call_responses_api(self, message, model, stream, prompt, history=None, image_path="", image_url="", image_paths=None):
        if stream:
            log(level="DEBUG", message=f"API调用模式（{self._log_label('Responses API', model=model)}）：Responses API 当前按非流式模式调用")
        normalized_paths = self._normalize_image_paths(image_path, image_paths)
        input_payload = []
        if prompt and str(prompt).strip():
            input_payload.append({"role": "system", "content": prompt})
        if history:
            for item in history:
                input_payload.append(format_history_message(item))
        if normalized_paths or image_url:
            content = [{"type": "input_text", "text": message}]
            if normalized_paths:
                content.extend(self._build_responses_image_block(path, "") for path in normalized_paths)
            else:
                content.append(self._build_responses_image_block("", image_url))
            input_payload.append({
                "role": "user",
                "content": content,
            })
        else:
            input_payload.append({"role": "user", "content": message})
        response = self.client.responses.create(
            model=model,
            input=input_payload,
            reasoning={"effort": normalize_reasoning_effort(getattr(self.config, "reasoning_effort", ""))},
        )
        text = self._extract_responses_text(response)
        if text:
            return text
        raise ValueError("Responses API 响应内容为空")

    @staticmethod
    def _extract_responses_text(response):
        output_text = getattr(response, "output_text", None)
        if isinstance(output_text, str) and output_text:
            return output_text
        output = getattr(response, "output", None)
        if not output:
            return None
        result_parts = []
        for output_item in output:
            content = getattr(output_item, "content", None)
            if not content:
                continue
            for block in content:
                text = getattr(block, "text", None)
                if isinstance(text, str) and text:
                    result_parts.append(text)
        if result_parts:
            return "".join(result_parts)
        return None


class DusAPI:
    def __init__(self, config):
        self.config = config
        self.DS_NOW_MOD = config.model
        self.api_key = config.key
        self.base_url = config.url.rstrip("/")
        self.max_retries = int(getattr(config, "max_retries", 5))
        self.request_timeout = MAIN_API_REQUEST_TIMEOUT_SECONDS
        self.max_output_tokens = max(1, int(getattr(config, "max_output_tokens", DEFAULT_CHAT_MAX_OUTPUT_TOKENS) or DEFAULT_CHAT_MAX_OUTPUT_TOKENS))
        self.reasoning_effort = normalize_reasoning_effort(getattr(config, "reasoning_effort", ""))
        self.last_error = None

    def _log_label(self, api_name="", *, model=None):
        return _api_log_label(self.config, api_name, model=model)

    def _log_failure(self, api_name, model, error, *, action, enabled):
        if enabled:
            log(
                level="WARNING",
                message=format_api_error_log_message(
                    self._log_label(api_name, model=model), error, action=action,
                ),
            )

    @staticmethod
    def build_image_block(image_path: str = "", image_url: str = "") -> dict:
        if image_path:
            image_path = prepare_ai_image_path(image_path)
            mime_type, _ = mimetypes.guess_type(image_path)
            if mime_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                mime_type = "image/jpeg"
            with open(image_path, "rb") as f:
                image_data = base64.standard_b64encode(f.read()).decode("utf-8")
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": image_data,
                },
            }
        if image_url:
            return {
                "type": "image",
                "source": {
                    "type": "url",
                    "url": image_url,
                },
            }
        raise ValueError("image_path 和 image_url 不能同时为空")

    @staticmethod
    def _build_gpt_image_block(image_path: str = "", image_url: str = "") -> dict:
        if image_path:
            image_path = prepare_ai_image_path(image_path)
            mime_type, _ = mimetypes.guess_type(image_path)
            if mime_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                mime_type = "image/jpeg"
            with open(image_path, "rb") as f:
                image_data = base64.standard_b64encode(f.read()).decode("utf-8")
            return {
                "type": "input_image",
                "image_url": f"data:{mime_type};base64,{image_data}",
            }
        if image_url:
            return {
                "type": "input_image",
                "image_url": image_url,
            }
        raise ValueError("image_path 和 image_url 不能同时为空")

    @staticmethod
    def _normalize_image_paths(image_path: str = "", image_paths=None):
        normalized = [str(path or "").strip() for path in (image_paths or []) if str(path or "").strip()]
        if normalized:
            return normalized
        single = str(image_path or "").strip()
        return [single] if single else []

    @staticmethod
    def _extract_gpt_text(response_data: dict):
        try:
            output_text = response_data.get("output_text")
            if isinstance(output_text, str) and output_text:
                return output_text
            output = response_data.get("output", [])
            result_parts = []
            if isinstance(output, list):
                for item in output:
                    if not isinstance(item, dict) or item.get("type") != "message":
                        continue
                    content = item.get("content", [])
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") in ("output_text", "text"):
                            text = block.get("text")
                            if text:
                                result_parts.append(text)
            if result_parts:
                return "".join(result_parts)
            return None
        except Exception:
            return None

    def _stream_claude_text(self, api_endpoint, headers, payload) -> str:
        response = requests.post(api_endpoint, headers=headers, json=payload, timeout=self.request_timeout, stream=True)
        response.raise_for_status()
        response.encoding = "utf-8"
        result_parts = []
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line or not raw_line.startswith("data:"):
                continue
            data_str = raw_line[5:].strip()
            if not data_str:
                continue
            try:
                data = json.loads(data_str)
            except Exception:
                continue
            if data.get("type") == "content_block_delta":
                delta = data.get("delta", {})
                text = delta.get("text")
                if text:
                    result_parts.append(text)
            elif data.get("type") == "message_stop":
                break
        return "".join(result_parts)

    def _stream_gpt_text(self, api_endpoint, headers, payload) -> str:
        response = requests.post(api_endpoint, headers=headers, json=payload, timeout=self.request_timeout, stream=True)
        response.encoding = "utf-8"
        if response.status_code >= 400:
            raise Exception(
                f"GPT接口请求失败，status={response.status_code}, "
                f"response={_truncate_log_text(response.text, 500)}, "
                f"payload_summary={_summarize_request_payload(payload)}"
            )
        result_parts = []
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line or not raw_line.startswith("data:"):
                continue
            data_str = raw_line[5:].strip()
            if not data_str:
                continue
            if data_str == "[DONE]":
                break
            try:
                data = json.loads(data_str)
            except Exception:
                continue
            event_type = data.get("type")
            if event_type in ("response.output_text.delta", "response.refusal.delta"):
                delta = data.get("delta")
                if isinstance(delta, str) and delta:
                    result_parts.append(delta)
            elif event_type == "response.completed":
                try:
                    output_text = data.get("response", {}).get("output_text")
                    if isinstance(output_text, str) and output_text and not result_parts:
                        result_parts.append(output_text)
                except Exception:
                    pass
        return "".join(result_parts)

    def chat(
        self,
        message,
        model=None,
        stream=True,
        prompt=None,
        history=None,
        image_path: str = "",
        image_url: str = "",
        image_paths=None,
        log_errors=True,
    ):
        if model is None:
            model = self.DS_NOW_MOD
        if prompt is None:
            prompt = self.config.prompt
        retry_delays = [2, 4, 8, 16, 32]
        max_retries = max(0, min(self.max_retries, len(retry_delays)))
        last_error = None
        self.last_error = None
        normalized_paths = self._normalize_image_paths(image_path, image_paths)

        if "claude" in model.lower():
            headers = {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
                "user-agent": _chat_api_user_agent(),
            }
            if normalized_paths or image_url:
                user_content = []
                if normalized_paths:
                    user_content.extend(self.build_image_block(path, "") for path in normalized_paths)
                else:
                    user_content.append(self.build_image_block("", image_url))
                user_content.append({"type": "text", "text": message})
            else:
                user_content = message
            messages = []
            if history:
                for h in history:
                    messages.append(format_history_message(h))
            messages.append({"role": "user", "content": user_content})
            payload = {
                "model": model,
                "max_tokens": self.max_output_tokens,
                "system": prompt,
                "messages": messages,
            }
            api_endpoint = f"{self.base_url}/v1/messages"
            if stream:
                payload["stream"] = True
                for attempt in range(max_retries + 1):
                    try:
                        result = self._stream_claude_text(api_endpoint, headers, payload)
                        if result:
                            return result
                        raise ValueError("DusAPI Claude 流式响应中未找到文本内容")
                    except Exception as e:
                        last_error = e
                        self.last_error = e
                        if attempt < max_retries:
                            delay = retry_delays[attempt]
                            self._log_failure(
                                'DusAPI Claude', model, e,
                                action=f"{delay} 秒后重试（第 {attempt + 1} 次）", enabled=log_errors,
                            )
                            time.sleep(delay)
                        else:
                            self._log_failure(
                                'DusAPI Claude', model, last_error,
                                action=f"已重试 {max_retries} 次，本次调用已停止", enabled=log_errors,
                            )
                return API_ERROR_REPLY_TEXT

            for attempt in range(max_retries + 1):
                try:
                    response = requests.post(api_endpoint, headers=headers, json=payload, timeout=self.request_timeout)
                    response.raise_for_status()
                    response.encoding = "utf-8"
                    response_data = response.json()
                    result = response_data["content"][0]["text"]
                    return result
                except Exception as e:
                    last_error = e
                    self.last_error = e
                    if attempt < max_retries:
                        delay = retry_delays[attempt]
                        self._log_failure(
                            'DusAPI Claude', model, e,
                            action=f"{delay} 秒后重试（第 {attempt + 1} 次）", enabled=log_errors,
                        )
                        time.sleep(delay)
                    else:
                        self._log_failure(
                            'DusAPI Claude', model, last_error,
                            action=f"已重试 {max_retries} 次，本次调用已停止", enabled=log_errors,
                        )
            return API_ERROR_REPLY_TEXT

        if "gpt" in model.lower():
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "content-type": "application/json",
                "user-agent": _chat_api_user_agent(),
            }
            input_items = []
            if prompt:
                input_items.append({"role": "system", "content": prompt})
            if history:
                for h in history:
                    input_items.append(format_history_message(h))
            if normalized_paths or image_url:
                user_content = [
                    {"type": "input_text", "text": message},
                ]
                if normalized_paths:
                    user_content.extend(self._build_gpt_image_block(path, "") for path in normalized_paths)
                else:
                    user_content.append(self._build_gpt_image_block("", image_url))
            else:
                user_content = message
            input_items.append({"role": "user", "content": user_content})
            payload = {
                "model": model,
                "input": input_items,
                "max_output_tokens": self.max_output_tokens,
                "reasoning": {"effort": self.reasoning_effort},
            }
            api_endpoint = f"{self.base_url}/v1/responses"
            if stream:
                payload["stream"] = True
                for attempt in range(max_retries + 1):
                    try:
                        result = self._stream_gpt_text(api_endpoint, headers, payload)
                        if result:
                            return result
                        raise ValueError("DusAPI GPT 流式响应中未找到文本内容")
                    except Exception as e:
                        last_error = e
                        self.last_error = e
                        if attempt < max_retries:
                            delay = retry_delays[attempt]
                            self._log_failure(
                                'DusAPI GPT', model, e,
                                action=f"{delay} 秒后重试（第 {attempt + 1} 次）", enabled=log_errors,
                            )
                            time.sleep(delay)
                        else:
                            self._log_failure(
                                'DusAPI GPT', model, last_error,
                                action=f"已重试 {max_retries} 次，本次调用已停止", enabled=log_errors,
                            )
                return API_ERROR_REPLY_TEXT

            for attempt in range(max_retries + 1):
                try:
                    response = requests.post(api_endpoint, headers=headers, json=payload, timeout=self.request_timeout)
                    response.raise_for_status()
                    response.encoding = "utf-8"
                    response_data = response.json()
                    result = self._extract_gpt_text(response_data)
                    if result is None:
                        raise ValueError(
                            "DusAPI GPT 响应中未找到文本内容，"
                            f"response_summary={_summarize_response_data(response_data)}"
                        )
                    return result
                except Exception as e:
                    last_error = e
                    self.last_error = e
                    if attempt < max_retries:
                        delay = retry_delays[attempt]
                        self._log_failure(
                            'DusAPI GPT', model, e,
                            action=f"{delay} 秒后重试（第 {attempt + 1} 次）", enabled=log_errors,
                        )
                        time.sleep(delay)
                    else:
                        self._log_failure(
                            'DusAPI GPT', model, last_error,
                            action=f"已重试 {max_retries} 次，本次调用已停止", enabled=log_errors,
                        )
            return API_ERROR_REPLY_TEXT

        self.last_error = ValueError(f"DusAPI 未识别模型：{model}")
        if log_errors:
            log(level="WARNING", message=f"DusAPI 无法识别模型 {model}，请检查模型名称是否包含 gpt 或 claude")
        return API_ERROR_REPLY_TEXT


def default_tts_config():
    sdk = default_tts_sdk()
    sdk_meta = get_tts_sdk_meta(sdk)
    model = default_tts_model(sdk)
    model_meta = get_tts_model_meta(sdk, model)
    return {
        "sdk": sdk,
        "name": "语音接口",
        "model": model,
        "model_label": str(model_meta.get("label") or model or "语音模型"),
        "url": str(sdk_meta.get("default_url") or "").strip(),
        "enabled": True,
        "credentials": {"api_key": ""},
        "voice_id": "",
        "voice_name": "",
        "sample_text": "你好呀，这是一条语音回复试听。",
        "voice_list_cache": [],
        "voice_list_updated_at": "",
    }


def normalize_tts_settings(config):
    config = config if isinstance(config, dict) else {}
    raw_tts_configs = config.get("tts_configs")
    if not isinstance(raw_tts_configs, list) or not raw_tts_configs:
        raw_tts_configs = [default_tts_config()]
    normalized_tts_configs = []
    for item in raw_tts_configs:
        source = item if isinstance(item, dict) else {}
        normalized = default_tts_config()
        normalized["sdk"] = resolve_tts_sdk(source) or normalized["sdk"]
        sdk_meta = get_tts_sdk_meta(normalized["sdk"])
        normalized["model"] = resolve_tts_model({"sdk": normalized["sdk"], "model": source.get("model")}) or normalized["model"]
        model_meta = get_tts_model_meta(normalized["sdk"], normalized["model"])
        normalized["name"] = "语音接口"
        normalized["model_label"] = str(model_meta.get("label") or normalized["model"] or "语音模型")
        normalized["url"] = str(source.get("url") or sdk_meta.get("default_url") or normalized.get("url") or "").strip()
        normalized["enabled"] = bool(source.get("enabled", normalized["enabled"]))
        credentials = source.get("credentials") if isinstance(source.get("credentials"), dict) else {}
        normalized["credentials"] = {"api_key": str(credentials.get("api_key") or "").strip()}
        normalized["voice_id"] = str(source.get("voice_id") or "").strip()
        normalized["voice_name"] = str(source.get("voice_name") or "").strip()
        normalized["sample_text"] = str(source.get("sample_text") or normalized["sample_text"]).strip() or normalized["sample_text"]
        voice_list_cache = source.get("voice_list_cache")
        normalized["voice_list_cache"] = voice_list_cache if isinstance(voice_list_cache, list) else []
        normalized["voice_list_updated_at"] = str(source.get("voice_list_updated_at") or "").strip()
        normalized_tts_configs.append(normalized)
    config["tts_configs"] = normalized_tts_configs
    try:
        tts_index = int(config.get("tts_index", 0))
    except (TypeError, ValueError):
        tts_index = 0
    config["tts_index"] = max(0, min(len(normalized_tts_configs) - 1, tts_index))
    return config


def select_tts_config(tts_configs, tts_index=0):
    configs = tts_configs if isinstance(tts_configs, list) else []
    if not configs:
        return {}
    try:
        idx = int(tts_index or 0)
    except (TypeError, ValueError):
        idx = 0
    idx = max(0, min(len(configs) - 1, idx))
    cfg = configs[idx]
    return cfg if isinstance(cfg, dict) else {}


def _looks_like_masked_key(value):
    text = str(value or "").strip()
    return bool(text) and "*" in text


def resolve_tts_preview_payload(payload, *, saved_config=None):
    resolved = dict(payload or {})
    credentials = resolved.get("credentials") if isinstance(resolved.get("credentials"), dict) else {}
    api_key = str(credentials.get("api_key") or "").strip()
    if api_key and not _looks_like_masked_key(api_key):
        resolved["credentials"] = {"api_key": api_key}
        return resolved

    saved = normalize_tts_settings(dict(saved_config or {}))
    tts_configs = saved.get("tts_configs") if isinstance(saved.get("tts_configs"), list) else []
    try:
        target_index = int(resolved.get("tts_index", saved.get("tts_index", 0)))
    except (TypeError, ValueError):
        target_index = int(saved.get("tts_index", 0) or 0)
    target_index = max(0, min(len(tts_configs) - 1, target_index)) if tts_configs else 0
    saved_item = tts_configs[target_index] if tts_configs else {}
    saved_credentials = saved_item.get("credentials") if isinstance(saved_item, dict) else {}
    saved_key = str((saved_credentials or {}).get("api_key") or "").strip()
    resolved["credentials"] = {"api_key": saved_key or api_key}
    return resolved


__all__ = [
    "APIConfigSnapshot",
    "API_PROTOCOL_VALUES",
    "DEFAULT_CHAT_MAX_OUTPUT_TOKENS",
    "DEFAULT_API_PROTOCOL",
    "DEFAULT_REASONING_EFFORT",
    "REASONING_EFFORT_VALUES",
    "DusAPI",
    "OpenAIAPI",
    "build_api_config_snapshot",
    "default_tts_config",
    "default_tts_model",
    "default_tts_sdk",
    "get_tts_sdk_meta",
    "list_tts_model_options",
    "list_tts_sdk_options",
    "normalize_tts_settings",
    "normalize_api_protocol",
    "normalize_reasoning_effort",
    "resolve_tts_model",
    "resolve_tts_preview_payload",
    "resolve_tts_sdk",
    "select_tts_config",
    "set_chat_api_app_version",
]
