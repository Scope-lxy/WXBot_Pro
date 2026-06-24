"""TTS runtime adapters and provider registry."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from uuid import uuid4

import requests

DEFAULT_TTS_ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"


class TTSConfigError(RuntimeError):
    """Raised when the configured TTS SDK cannot synthesize audio."""


def make_tts_cache_path(root, *, suffix="mp3"):
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    safe_suffix = str(suffix or "mp3").lstrip(".") or "mp3"
    return base / f"tts_{uuid4().hex}.{safe_suffix}"


class DoubaoTTSClient:
    def __init__(self, config):
        self.config = config or {}

    def validate(self):
        credentials = self.config.get("credentials") or {}
        if not str(credentials.get("api_key") or "").strip():
            raise TTSConfigError("请填写语音接口 API Key")
        if not str(self.config.get("voice_id") or "").strip():
            raise TTSConfigError("请选择或填写音色 ID")

    def _resource_id(self):
        return str(self.config.get("_tts_resource_id") or "seed-tts-2.0").strip() or "seed-tts-2.0"

    def _endpoint(self):
        return str(self.config.get("url") or DEFAULT_TTS_ENDPOINT).strip() or DEFAULT_TTS_ENDPOINT

    def _headers(self):
        credentials = self.config.get("credentials") or {}
        return {
            "Content-Type": "application/json",
            "X-Api-Key": str(credentials.get("api_key") or "").strip(),
            "X-Api-Resource-Id": self._resource_id(),
            "X-Api-Request-Id": str(uuid4()),
        }

    def _payload(self, text):
        additions = {}
        if bool(self.config.get("_tts_supports_context", True)):
            context_text = str(self.config.get("context_text") or "").strip()
            if context_text:
                additions["context_texts"] = [context_text]
            section_id = str(self.config.get("section_id") or "").strip()
            if section_id:
                additions["section_id"] = section_id
        request_model = str(self.config.get("_tts_request_model") or "").strip()
        return {
            "user": {
                "uid": str(self.config.get("uid") or "siverwxbot"),
            },
            "namespace": "BidirectionalTTS",
            "req_params": {
                "text": str(text or ""),
                "speaker": str(self.config.get("voice_id") or "").strip(),
                "audio_params": {
                    "format": str(self.config.get("format") or "mp3"),
                    "sample_rate": int(self.config.get("sample_rate") or 24000),
                },
                **({"model": request_model} if request_model else {}),
                **({"additions": json.dumps(additions, ensure_ascii=False)} if additions else {}),
            },
        }

    @staticmethod
    def _extract_audio_chunk(event):
        data = event.get("data")
        if isinstance(data, str) and data:
            return data
        if isinstance(data, dict):
            for key in ("audio", "chunk", "binary", "data"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
        return ""

    def synthesize(self, text, out_path):
        self.validate()
        response = requests.post(
            self._endpoint(),
            headers=self._headers(),
            json=self._payload(text),
            stream=True,
            timeout=60,
        )
        response.raise_for_status()

        audio_chunks = bytearray()
        final_message = ""
        for raw_line in response.iter_lines(decode_unicode=True):
            line = str(raw_line or "").strip()
            if not line:
                continue
            if line.startswith("event:"):
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line or line == "[DONE]":
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue

            code = payload.get("code")
            if code not in (0, 20000000, None):
                raise TTSConfigError(str(payload.get("message") or f"语音合成失败：{code}"))
            if payload.get("message"):
                final_message = str(payload.get("message") or "")

            audio_b64 = self._extract_audio_chunk(payload)
            if audio_b64:
                try:
                    audio_chunks.extend(base64.b64decode(audio_b64))
                except Exception as exc:  # pragma: no cover - decode failure is converted to a user-facing error
                    raise TTSConfigError(f"语音音频分片解析失败：{exc}") from exc

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(bytes(audio_chunks))
        if out_path.stat().st_size <= 0:
            raise TTSConfigError(final_message or "语音合成返回空文件")
        return out_path


TTS_SDK_REGISTRY = {
    "volcengine_openspeech": {
        "key": "volcengine_openspeech",
        "label": "火山引擎 OpenSpeech",
        "default_url": DEFAULT_TTS_ENDPOINT,
        "client_class": DoubaoTTSClient,
        "models": [
            {
                "key": "doubao_tts_2_0_standard",
                "label": "豆包语音合成 2.0 标准版（seed-tts-2.0-standard）",
                "resource_id": "seed-tts-2.0",
                "request_model": "seed-tts-2.0-standard",
                "supports_context": False,
                "voice_docs_url": "https://www.volcengine.com/docs/6561/2528925?lang=zh",
            },
            {
                "key": "doubao_tts_2_0_expressive",
                "label": "豆包语音合成 2.0 高表现力版（seed-tts-2.0-expressive）",
                "resource_id": "seed-tts-2.0",
                "request_model": "seed-tts-2.0-expressive",
                "supports_context": True,
                "voice_docs_url": "https://www.volcengine.com/docs/6561/2528925?lang=zh",
            },
            {
                "key": "doubao_tts_1_0",
                "label": "豆包语音合成 1.0（seed-tts-1.0）",
                "resource_id": "seed-tts-1.0",
                "supports_context": False,
                "voice_docs_url": "https://www.volcengine.com/docs/6561/2528925?lang=zh",
            },
            {
                "key": "doubao_icl_2_0",
                "label": "豆包复刻/设计音色 2.0（seed-icl-2.0）",
                "resource_id": "seed-icl-2.0",
                "supports_context": False,
                "voice_docs_url": "https://www.volcengine.com/docs/6561/2277844?lang=zh",
            },
        ],
    },
}


def list_tts_sdk_options():
    return [
        {"key": key, "label": str(meta.get("label") or key)}
        for key, meta in TTS_SDK_REGISTRY.items()
    ]


def default_tts_sdk():
    return next(iter(TTS_SDK_REGISTRY.keys()), "")


def get_tts_sdk_meta(sdk_key=None):
    key = str(sdk_key or "").strip()
    if key in TTS_SDK_REGISTRY:
        return TTS_SDK_REGISTRY[key]
    fallback = default_tts_sdk()
    return TTS_SDK_REGISTRY.get(fallback, {})


def list_tts_model_options(sdk_key=None):
    sdk_meta = get_tts_sdk_meta(sdk_key)
    return [
        {
            "key": str(model.get("key") or ""),
            "label": str(model.get("label") or model.get("key") or ""),
            "voice_docs_url": str(model.get("voice_docs_url") or ""),
        }
        for model in (sdk_meta.get("models") or [])
        if str(model.get("key") or "").strip()
    ]


def default_tts_model(sdk_key=None):
    options = list_tts_model_options(sdk_key)
    return options[0]["key"] if options else ""


def get_tts_model_meta(sdk_key=None, model_key=None):
    models = get_tts_sdk_meta(sdk_key).get("models") or []
    target = str(model_key or "").strip()
    for model in models:
        if str(model.get("key") or "").strip() == target:
            return model
    fallback = default_tts_model(sdk_key)
    for model in models:
        if str(model.get("key") or "").strip() == fallback:
            return model
    return {}


def resolve_tts_sdk(config):
    source = config if isinstance(config, dict) else {}
    sdk = str(source.get("sdk") or "").strip()
    if sdk in TTS_SDK_REGISTRY:
        return sdk
    return default_tts_sdk()


def resolve_tts_model(config):
    source = config if isinstance(config, dict) else {}
    sdk = resolve_tts_sdk(source)
    model = str(source.get("model") or "").strip()
    if model and model in {item["key"] for item in list_tts_model_options(sdk)}:
        return model
    return default_tts_model(sdk)


def create_tts_client(config):
    source = dict(config or {})
    sdk = resolve_tts_sdk(source)
    meta = get_tts_sdk_meta(sdk)
    model_key = resolve_tts_model(source)
    model_meta = get_tts_model_meta(sdk, model_key)
    client_class = meta.get("client_class")
    if client_class is None:
        raise TTSConfigError("当前语音模型暂不支持")
    source["sdk"] = sdk
    source["model"] = model_key
    source["url"] = str(source.get("url") or meta.get("default_url") or "").strip()
    source["_tts_resource_id"] = str(model_meta.get("resource_id") or "").strip()
    source["_tts_supports_context"] = bool(model_meta.get("supports_context", True))
    source["_tts_request_model"] = str(model_meta.get("request_model") or "").strip()
    return client_class(source)


__all__ = [
    "DEFAULT_TTS_ENDPOINT",
    "DoubaoTTSClient",
    "TTSConfigError",
    "create_tts_client",
    "default_tts_model",
    "default_tts_sdk",
    "get_tts_sdk_meta",
    "list_tts_model_options",
    "list_tts_sdk_options",
    "make_tts_cache_path",
    "resolve_tts_model",
    "resolve_tts_sdk",
]
