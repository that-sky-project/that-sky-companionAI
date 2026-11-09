# -*- coding: utf-8 -*-
"""各家 AI 接口的统一适配层（W2）。

对调用方只暴露 OpenAI 的 Chat Completions 形状：

    client.chat(messages, tools=None, max_tokens=300, temperature=0.85, timeout=30)
        -> {"role": "assistant", "content": "...", "tool_calls": [...]}
    client.models() -> ["..."]
    client.complete(prompt) -> "..."

支持两类接口：
  * OpenAI 兼容（DeepSeek / OpenAI / Kimi / 智谱 / 通义千问 / 豆包 / SiliconFlow /
    OpenRouter / Gemini / Ollama / LM Studio / 自定义）——同一个实现，只是预设不同；
  * Anthropic 原生 Messages API（/v1/messages，x-api-key + anthropic-version）。

不写死模型名：模型一律从 /models 拉，拉不到就让用户手填（配置里保存）。
Key 不写进 config.json：Windows 用 DPAPI 存 DATA/secrets/ai_key.bin，服务器存 0600
文件；读取时兼容 secrets.env / DEEPSEEK_API_KEY 等环境变量。
"""
from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import requests

import ai_secrets
import xg_paths

ANTHROPIC_VERSION = "2023-06-01"
CUSTOM = "custom"
_CORE_PARAMS = {"model", "messages", "tools", "max_tokens", "temperature", "system", "stream"}

# 预设只写：显示名、base_url、文档、是否要 key、额外请求参数。
# 模型名不写死（从 /models 拉，或用户手填）。
PRESETS: dict[str, dict] = {
    "deepseek": {
        "name": "DeepSeek（深度求索）",
        "api": "openai",
        "base_url": "https://api.deepseek.com",
        "docs": "https://api-docs.deepseek.com/",
        "key_url": "https://platform.deepseek.com/api_keys",
        "needs_key": True,
        "env_key": "DEEPSEEK_API_KEY",
        "env_model": "DEEPSEEK_MODEL",
        "env_base": "DEEPSEEK_BASE_URL",
        "extra": {"thinking": {"type": "disabled"}},
        "note": "国内直连；小光默认关掉思考模式，聊天更快。",
    },
    "openai": {
        "name": "OpenAI",
        "api": "openai",
        "base_url": "https://api.openai.com/v1",
        "docs": "https://platform.openai.com/docs/api-reference/chat",
        "key_url": "https://platform.openai.com/api-keys",
        "needs_key": True,
        "env_key": "OPENAI_API_KEY",
        "extra": {},
        "note": "需要能访问 OpenAI 的网络。",
    },
    "kimi": {
        "name": "Kimi（Moonshot）",
        "api": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "docs": "https://platform.moonshot.cn/docs/api/chat",
        "key_url": "https://platform.moonshot.cn/console/api-keys",
        "needs_key": True,
        "env_key": "MOONSHOT_API_KEY",
        "extra": {},
        "note": "国内可直连，OpenAI 兼容。",
    },
    "zhipu": {
        "name": "智谱 GLM",
        "api": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "docs": "https://docs.bigmodel.cn/cn/guide/develop/openai/introduction",
        "key_url": "https://bigmodel.cn/usercenter/proj-mgmt/apikeys",
        "needs_key": True,
        "env_key": "ZHIPU_API_KEY",
        "extra": {},
        "note": "OpenAI 兼容接口。",
    },
    "qwen": {
        "name": "通义千问（DashScope 兼容模式）",
        "api": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "docs": "https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope",
        "key_url": "https://bailian.console.aliyun.com/?apiKey=1",
        "needs_key": True,
        "env_key": "DASHSCOPE_API_KEY",
        "extra": {},
        "note": "阿里云百炼；海外地域把域名换成 dashscope-intl.aliyuncs.com。",
    },
    "doubao": {
        "name": "豆包（火山方舟）",
        "api": "openai",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "docs": "https://www.volcengine.com/docs/82379/1298454",
        "key_url": "https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
        "needs_key": True,
        "env_key": "ARK_API_KEY",
        "extra": {},
        "note": "火山方舟的模型 ID 可能是一串接入点 ID（ep-...），从 /models 拉不到时手填。",
    },
    "siliconflow": {
        "name": "硅基流动 SiliconFlow",
        "api": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "docs": "https://docs.siliconflow.cn/cn/api-reference/chat-completions/chat-completions",
        "key_url": "https://cloud.siliconflow.cn/account/ak",
        "needs_key": True,
        "env_key": "SILICONFLOW_API_KEY",
        "extra": {},
        "note": "聚合了很多开源模型，OpenAI 兼容。",
    },
    "openrouter": {
        "name": "OpenRouter",
        "api": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "docs": "https://openrouter.ai/docs/api-reference/overview",
        "key_url": "https://openrouter.ai/keys",
        "needs_key": True,
        "env_key": "OPENROUTER_API_KEY",
        "extra": {},
        "note": "聚合接口；国内访问可能需要代理。",
    },
    "gemini": {
        "name": "Gemini（Google OpenAI 兼容端点）",
        "api": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "docs": "https://ai.google.dev/gemini-api/docs/openai",
        "key_url": "https://aistudio.google.com/app/apikey",
        "needs_key": True,
        "env_key": "GEMINI_API_KEY",
        "extra": {},
        "note": "Google 的 OpenAI 兼容层；需要能访问 Google。",
    },
    "ollama": {
        "name": "Ollama（本机）",
        "api": "openai",
        "base_url": "http://127.0.0.1:11434/v1",
        "docs": "https://github.com/ollama/ollama/blob/main/docs/openai.md",
        "key_url": "",
        "needs_key": False,
        "extra": {},
        "note": "本机跑，不用 Key；先 ollama serve，再拉模型。",
    },
    "lmstudio": {
        "name": "LM Studio（本机）",
        "api": "openai",
        "base_url": "http://127.0.0.1:1234/v1",
        "docs": "https://lmstudio.ai/docs/app/api/endpoints/openai",
        "key_url": "",
        "needs_key": False,
        "extra": {},
        "note": "本机跑，不用 Key；在 LM Studio 里开 Local Server。",
    },
    "anthropic": {
        "name": "Anthropic Claude（原生 Messages API）",
        "api": "anthropic",
        "base_url": "https://api.anthropic.com",
        "docs": "https://docs.anthropic.com/en/api/messages",
        "key_url": "https://console.anthropic.com/settings/keys",
        "needs_key": True,
        "env_key": "ANTHROPIC_API_KEY",
        "extra": {},
        "note": "官方原生接口（/v1/messages），不是 OpenAI 兼容层。",
    },
    CUSTOM: {
        "name": "自定义（OpenAI 兼容）",
        "api": "openai",
        "base_url": "",
        "docs": "",
        "key_url": "",
        "needs_key": False,
        "extra": {},
        "note": "自己填接口地址（要包含 /v1 一类的版本前缀）。",
    },
}


class ProviderError(RuntimeError):
    """可以直接展示给用户的中文错误；不含 key。"""

    def __init__(self, message: str, status: int | None = None, kind: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.kind = kind


def _scrub(text: str, key: str = "") -> str:
    text = str(text or "")
    if key:
        text = text.replace(key, "***")
    return re.sub(r"sk-[A-Za-z0-9_-]{8,}", "sk-***", text)


def get_preset(provider: str) -> dict:
    p = PRESETS.get(provider)
    if p is None:
        raise ProviderError(f"不认识的 AI 服务商：{provider}")
    return copy.deepcopy(p)


def preset_list() -> list[dict]:
    out = []
    for pid, p in PRESETS.items():
        out.append({
            "id": pid, "name": p["name"], "api": p["api"], "base_url": p["base_url"],
            "docs": p.get("docs", ""), "key_url": p.get("key_url", ""),
            "needs_key": bool(p.get("needs_key")), "note": p.get("note", ""),
        })
    return out


_FROM_FILE = set()   # 从 DATA/secrets.env 明确读进来的变量名


def _env_blocked() -> bool:
    """Windows 安装版默认不信任从系统环境继承来的 Key（XG_ALLOW_ENV_KEYS=1 可放开）。"""
    return os.name == "nt" and os.environ.get("XG_ALLOW_ENV_KEYS") != "1"


def _load_secrets_env() -> None:
    """兼容原有 secrets.env：只补环境变量，不覆盖已有值。

    W1 读的是 DATA/secrets.env（服务器上 DATA==ROOT）；这里先 DATA，再回落 ROOT。
    """
    paths = [Path(xg_paths.DATA) / "secrets.env", Path(xg_paths.ROOT) / "secrets.env"]
    seen = set()
    for path in paths:
        if str(path) in seen:
            continue
        seen.add(str(path))
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip("\"'")
            if not k:
                continue
            if _env_blocked():
                # Windows：系统环境里继承来的同名变量不算数，secrets.env 里明确写的才算
                if k not in _FROM_FILE:
                    os.environ[k] = v
                    _FROM_FILE.add(k)
            elif k not in os.environ:
                os.environ[k] = v


@dataclass
class AiSettings:
    provider: str = "deepseek"
    base_url: str = ""
    model: str = ""
    key: str = ""
    extra: dict = field(default_factory=dict)
    api: str = "openai"
    name: str = ""
    needs_key: bool = True
    docs: str = ""
    key_url: str = ""
    note: str = ""
    key_error: str = ""
    key_source: str = ""

    @property
    def configured(self) -> bool:
        if not self.provider or not self.base_url or not self.model:
            return False
        if self.needs_key and not self.key:
            return False
        return True


def _config_ai() -> dict:
    cfg = xg_paths.config() or {}
    ai = cfg.get("ai")
    return dict(ai) if isinstance(ai, dict) else {}


def _env_key(preset: dict) -> str:
    # 2026-11-08 Claude：Windows 安装版默认不读环境变量里的 Key——用户电脑上可能本来就设着别的
    # 软件的 OPENAI_API_KEY 等，不能被小光悄悄拿去用。服务器（非 Windows）照旧兼容；
    # Windows 上确实想用环境变量时设 XG_ALLOW_ENV_KEYS=1。
    names = [preset.get("env_key") or "", "XG_AI_API_KEY"]
    if preset.get("api") == "anthropic":
        names.append("ANTHROPIC_API_KEY")
    if preset.get("env_base") == "DEEPSEEK_BASE_URL":
        names.append("DEEPSEEK_API_KEY")
    for n in names:
        if n and os.environ.get(n) and (not _env_blocked() or n in _FROM_FILE):
            return str(os.environ[n])
    return ""


def _stored_key() -> tuple[str, str]:
    try:
        return ai_secrets.load_key(), ""
    except ai_secrets.SecretError as exc:
        return "", str(exc)


def load_settings(payload: dict | None = None, *, with_key: bool = True) -> AiSettings:
    """读配置 + 预设 + Key。

    payload 可以覆盖 provider/base_url/model/extra/key（用于“测试”与“拉模型列表”，
    不保存）。payload 里没给 key 时，沿用已保存的 Key。
    """
    _load_secrets_env()
    payload = dict(payload or {})
    ai = _config_ai()
    provider = str(payload.get("provider") or ai.get("provider") or "deepseek").strip()
    if not provider:
        provider = "deepseek"
    if provider not in PRESETS:
        provider = CUSTOM
    preset = get_preset(provider)

    base = str(payload.get("base_url") or ai.get("base_url") or "").strip()
    if not base:
        base = str(os.environ.get(preset.get("env_base") or "") or preset.get("base_url") or "").strip()
    model = str(payload.get("model") or ai.get("model") or "").strip()
    if not model and preset.get("env_model"):
        model = str(os.environ.get(preset["env_model"]) or "").strip()

    extra = {}
    extra.update(preset.get("extra") or {})
    if provider == "deepseek" and str(os.environ.get("DEEPSEEK_THINKING", "")).lower() in ("1", "true", "yes", "on"):
        extra["thinking"] = {"type": "enabled"}
    if isinstance(ai.get("extra"), dict):
        extra.update(ai["extra"])
    if isinstance(payload.get("extra"), dict):
        extra.update(payload["extra"])

    key, key_error, source = "", "", ""
    explicit = payload.get("key")
    stored, stored_error = _stored_key() if with_key else ("", "")
    if not with_key:
        key = ""
    elif isinstance(explicit, str) and explicit.strip():
        key, source = explicit.strip(), "payload"
    elif ai.get("api_key"):
        key, source = str(ai.get("api_key")).strip(), "config(legacy)"
    elif stored:
        key, source = stored, "stored"
    else:
        env_key = _env_key(preset)
        if env_key:
            key, source = env_key, "env"
        else:
            key_error = stored_error

    return AiSettings(
        provider=provider, base_url=base.rstrip("/"), model=model, key=key, extra=extra,
        api=preset["api"], name=preset["name"], needs_key=bool(preset.get("needs_key")),
        docs=preset.get("docs", ""), key_url=preset.get("key_url", ""), note=preset.get("note", ""),
        key_error=key_error, key_source=source,
    )


def mask_key(key: str) -> str:
    return ("已设置（末 4 位 " + key[-4:] + "）") if key else "未设置"


def settings_status(settings: AiSettings) -> dict:
    """页面/接口用的状态；绝不返回 key。"""
    return {
        "ok": True,
        "provider": settings.provider,
        "name": settings.name,
        "api": settings.api,
        "base_url": settings.base_url,
        "model": settings.model,
        "has_key": bool(settings.key),
        "key_hint": mask_key(settings.key),
        "key_masked": mask_key(settings.key),
        "needs_key": settings.needs_key,
        "configured": settings.configured,
        "extra": {k: v for k, v in settings.extra.items()},
        "docs": settings.docs,
        "key_url": settings.key_url,
        "note": settings.note,
        "key_error": _scrub(settings.key_error, settings.key),
    }


def save_settings(payload: dict) -> AiSettings:
    """保存 ai 段；key 为空表示不改。key 永远不写进 config.json。"""
    payload = dict(payload or {})
    provider = str(payload.get("provider") or "").strip()
    if provider not in PRESETS:
        raise ProviderError("请选择一个服务商")
    preset = get_preset(provider)
    base = str(payload.get("base_url") or preset.get("base_url") or "").strip().rstrip("/")
    model = str(payload.get("model") or "").strip()
    if not base:
        raise ProviderError("接口地址不能为空")
    if not model:
        raise ProviderError("先选或填一个模型")
    extra = payload.get("extra")
    if extra is not None and not isinstance(extra, dict):
        raise ProviderError("额外参数必须是 JSON 对象")
    cfg = xg_paths.config() or {}
    old_ai = cfg.get("ai") if isinstance(cfg.get("ai"), dict) else {}
    ai = {"provider": provider, "base_url": base, "model": model}
    merged_extra = {}
    if isinstance(old_ai.get("extra"), dict):
        merged_extra.update(old_ai["extra"])
    if isinstance(extra, dict):
        merged_extra.update(extra)
    if merged_extra:
        ai["extra"] = merged_extra
    cfg["ai"] = ai
    key = payload.get("key")
    if isinstance(key, str) and key.strip():
        try:
            ai_secrets.store_key(key.strip())
        except ai_secrets.SecretError as exc:
            raise ProviderError(str(exc)) from exc
    elif payload.get("clear_key") is True:
        try:
            ai_secrets.clear_key()
        except ai_secrets.SecretError as exc:
            raise ProviderError(str(exc)) from exc
    xg_paths.save_config(cfg)
    return load_settings()


def _content_text(content) -> str:
    """把 content 统一成字符串（有的兼容层会返回 [{type:text,text:...}]）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict) and b.get("type") in (None, "text"):
                parts.append(str(b.get("text") or ""))
        return "\n".join(p for p in parts if p)
    return str(content)


def _messages_for(messages) -> list[dict]:
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        item = {"role": m.get("role", "user")}
        content = m.get("content")
        item["content"] = "" if content is None else content
        for k in ("tool_calls", "tool_call_id", "name"):
            if m.get(k) is not None:
                item[k] = m[k]
        out.append(item)
    return out


def _error_from_response(r: requests.Response, key: str) -> ProviderError:
    code = int(r.status_code)
    if code in (401, 403):
        return ProviderError(f"Key 不对或没权限（HTTP {code}），检查 Key 是否填对、是否开通了这个模型", code, "auth")
    if code == 404:
        return ProviderError("地址或模型名不对（HTTP 404），检查接口地址和模型名", code, "not_found")
    if code == 429:
        return ProviderError("额度/频率超限（HTTP 429），稍后再试或换一个 Key", code, "rate")
    body = ""
    try:
        body = _scrub(r.text[:240], key)
    except Exception:
        pass
    return ProviderError(f"接口返回 HTTP {code}" + (f"：{body}" if body else ""), code, "http")


def _host_of(url: str) -> str:
    try:
        return re.sub(r"^https?://", "", url).split("/")[0]
    except Exception:
        return ""


def _request(method: str, url: str, *, headers: dict, body: dict | None, timeout, key: str):
    try:
        return requests.request(method, url, headers=headers, json=body, timeout=timeout)
    except requests.exceptions.ConnectTimeout as exc:
        host = _host_of(url)
        raise ProviderError("连不上 " + (host or "接口地址") + "，检查网络、地址或代理", None, "connect") from exc
    except requests.exceptions.Timeout as exc:
        raise ProviderError(f"请求超时（{timeout} 秒），检查网络或换个接口地址", None, "timeout") from exc
    except requests.exceptions.SSLError as exc:
        raise ProviderError("HTTPS 证书校验失败，检查网络/代理或换一个接口地址", None, "ssl") from exc
    except requests.exceptions.ConnectionError as exc:
        host = _host_of(url)
        raise ProviderError("连不上 " + (host or "接口地址") + "，检查网络、地址或代理", None, "connect") from exc
    except requests.exceptions.RequestException as exc:
        raise ProviderError("请求没有完成：" + _scrub(str(exc), key)[:200], None, "request") from exc


def _json_or_error(r: requests.Response, key: str) -> dict:
    if r.status_code >= 400:
        raise _error_from_response(r, key)
    try:
        return r.json()
    except ValueError:
        raise ProviderError("接口返回的不是 JSON，地址可能不对", r.status_code, "json")


def _model_ids(doc) -> list[str]:
    rows = doc.get("data") if isinstance(doc, dict) else doc
    out = []
    if isinstance(rows, list):
        for x in rows:
            if isinstance(x, str):
                out.append(x)
            elif isinstance(x, dict):
                mid = x.get("id") or x.get("name") or x.get("model")
                if mid:
                    out.append(str(mid))
    seen, uniq = set(), []
    for m in out:
        if m not in seen:
            seen.add(m)
            uniq.append(m)
    return uniq


class OpenAICompatClient:
    """OpenAI 兼容的 /chat/completions（含 tools 函数调用）。"""

    def __init__(self, settings: AiSettings):
        self.s = settings

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.s.key:
            h["Authorization"] = "Bearer " + self.s.key
        return h

    def chat(self, messages, tools=None, max_tokens=300, temperature=0.85, timeout=30):
        if not self.s.model:
            raise ProviderError("还没有选模型：打开“设置 → AI 接口”，先拉模型列表或手填模型名")
        if self.s.needs_key and not self.s.key:
            raise ProviderError("还没有填 API Key：打开“设置 → AI 接口”填一个")
        body = {"model": self.s.model, "messages": _messages_for(messages),
                "max_tokens": int(max_tokens), "temperature": float(temperature)}
        if tools:
            body["tools"] = tools
        for k, v in (self.s.extra or {}).items():
            if k not in _CORE_PARAMS:
                body[k] = v
        url = self.s.base_url.rstrip("/") + "/chat/completions"
        r = _request("POST", url, headers=self._headers(), body=body, timeout=timeout, key=self.s.key)
        doc = _json_or_error(r, self.s.key)
        try:
            msg = doc["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("接口返回里没有 choices[0].message，地址或模型可能不对")
        if not isinstance(msg, dict):
            raise ProviderError("接口返回的 message 形状不对")
        out = {"role": "assistant", "content": _content_text(msg.get("content"))}
        if msg.get("tool_calls"):
            out["tool_calls"] = msg["tool_calls"]
        return out

    def models(self) -> list[str]:
        if self.s.needs_key and not self.s.key:
            raise ProviderError("还没有填 API Key")
        url = self.s.base_url.rstrip("/") + "/models"
        r = _request("GET", url, headers=self._headers(), body=None, timeout=20, key=self.s.key)
        return _model_ids(_json_or_error(r, self.s.key))

    def complete(self, prompt: str) -> str:
        return self.chat([{"role": "user", "content": prompt}], max_tokens=900,
                         temperature=0.3, timeout=60).get("content") or ""


# ------------------------------------------------------------------ Anthropic
def _anthropic_tools(tools) -> list[dict]:
    out = []
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) and isinstance(t.get("function"), dict) else t
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        out.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or fn.get("input_schema") or {"type": "object", "properties": {}},
        })
    return out


def _content_blocks(content) -> list[dict]:
    if isinstance(content, list):
        return [dict(b) for b in content if isinstance(b, dict)]
    return [{"type": "text", "text": "" if content is None else str(content)}]


def to_anthropic_messages(messages) -> tuple[str, list[dict]]:
    """OpenAI messages/tools → Anthropic system + messages（连续同角色合并）。"""
    system_parts: list[str] = []
    out: list[dict] = []

    def add(role: str, blocks: list[dict], tool_result: bool = False):
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            existing = out[-1]["content"]
            if tool_result:
                # tool_result 必须排在 user 消息内容的最前面
                at = 0
                while at < len(existing) and existing[at].get("type") == "tool_result":
                    at += 1
                existing[at:at] = blocks
            else:
                existing.extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "user"
        if role == "system":
            text = m.get("content")
            if isinstance(text, list):
                text = "\n".join(str(b.get("text", "")) for b in text if isinstance(b, dict))
            if text:
                system_parts.append(str(text))
            continue
        if role == "tool":
            add("user", [{
                "type": "tool_result",
                "tool_use_id": str(m.get("tool_call_id") or ""),
                "content": "" if m.get("content") is None else str(m.get("content")),
            }], tool_result=True)
            continue
        if role == "assistant":
            blocks = []
            text = m.get("content")
            if text not in (None, ""):
                blocks.extend(_content_blocks(text))
            for tc in m.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                raw = fn.get("arguments")
                try:
                    args = json.loads(raw) if isinstance(raw, str) else (raw or {})
                except ValueError:
                    args = {"_raw": raw}
                blocks.append({
                    "type": "tool_use",
                    "id": str(tc.get("id") or ""),
                    "name": str(fn.get("name") or ""),
                    "input": args if isinstance(args, dict) else {"value": args},
                })
            add("assistant", blocks)
            continue
        add("user", _content_blocks(m.get("content")))

    # 去掉空消息，避免 Anthropic 报 content 不能为空
    cleaned = []
    for m in out:
        content = [b for b in m["content"] if not (b.get("type") == "text" and str(b.get("text", "")) == "")]
        if content:
            cleaned.append({"role": m["role"], "content": content})
    return "\n\n".join(system_parts), cleaned


class AnthropicClient:
    """Anthropic 原生 Messages API。"""

    def __init__(self, settings: AiSettings):
        self.s = settings

    def _base(self) -> str:
        b = self.s.base_url.rstrip("/")
        return b[:-3] if b.endswith("/v1") else b

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "anthropic-version": ANTHROPIC_VERSION}
        if self.s.key:
            h["x-api-key"] = self.s.key
        return h

    def chat(self, messages, tools=None, max_tokens=300, temperature=0.85, timeout=30):
        if not self.s.model:
            raise ProviderError("还没有选模型：打开“设置 → AI 接口”，先拉模型列表或手填模型名")
        if not self.s.key:
            raise ProviderError("还没有填 API Key：打开“设置 → AI 接口”填一个")
        system, msgs = to_anthropic_messages(messages)
        body = {"model": self.s.model, "max_tokens": int(max_tokens),
                "temperature": float(temperature), "messages": msgs or [{"role": "user", "content": [{"type": "text", "text": ""}]}]}
        if system:
            body["system"] = system
        conv = _anthropic_tools(tools)
        if conv:
            body["tools"] = conv
        for k, v in (self.s.extra or {}).items():
            if k not in _CORE_PARAMS:
                body[k] = v
        url = self._base() + "/v1/messages"
        r = _request("POST", url, headers=self._headers(), body=body, timeout=timeout, key=self.s.key)
        doc = _json_or_error(r, self.s.key)
        blocks = doc.get("content")
        if not isinstance(blocks, list):
            raise ProviderError("接口返回里没有 content，地址或模型可能不对")
        texts, calls = [], []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                texts.append(str(b.get("text") or ""))
            elif b.get("type") == "tool_use":
                calls.append({
                    "id": str(b.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(b.get("name") or ""),
                        "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False),
                    },
                })
        out = {"role": "assistant", "content": "\n".join(t for t in texts if t)}
        if calls:
            out["tool_calls"] = calls
        return out

    def models(self) -> list[str]:
        if not self.s.key:
            raise ProviderError("还没有填 API Key")
        url = self._base() + "/v1/models"
        r = _request("GET", url, headers=self._headers(), body=None, timeout=20, key=self.s.key)
        return _model_ids(_json_or_error(r, self.s.key))

    def complete(self, prompt: str) -> str:
        return self.chat([{"role": "user", "content": prompt}], max_tokens=900,
                         temperature=0.3, timeout=60).get("content") or ""


def client_for(settings: AiSettings):
    return AnthropicClient(settings) if settings.api == "anthropic" else OpenAICompatClient(settings)


# 模块级快捷入口（给不想自己建 client 的调用方；ai_companion 走 LLM 薄封装）
def chat(messages, tools=None, max_tokens=300, temperature=0.85, timeout=30, settings=None):
    s = settings or load_settings()
    return client_for(s).chat(messages, tools=tools, max_tokens=max_tokens,
                              temperature=temperature, timeout=timeout)


def models(settings=None):
    s = settings or load_settings()
    return client_for(s).models()


def complete(prompt, settings=None):
    s = settings or load_settings()
    return client_for(s).complete(prompt)


# ------------------------------------------------------------------ 供后台用
def models_for(payload: dict | None = None) -> dict:
    try:
        s = load_settings(payload)
        if s.needs_key and not s.key:
            raise ProviderError(s.key_error or "还没有填 API Key")
        return {"ok": True, "models": client_for(s).models(), "provider": s.provider}
    except ProviderError as exc:
        return {"ok": False, "error": exc.message, "status": exc.status, "kind": exc.kind}
    except ai_secrets.SecretError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:                       # 不让页面看到 Python 栈
        return {"ok": False, "error": _scrub(f"拉模型列表失败：{type(exc).__name__}")}


TEST_TOOL = {
    "type": "function",
    "function": {
        "name": "xg_ping",
        "description": "小光接口测试用的假工具。收到测试请求时必须调用它。",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "固定填 pong"}},
            "required": ["text"],
        },
    },
}


def test_for(payload: dict | None = None) -> dict:
    """发一条最小对话 + 一个假工具，报告连通/回复/是否支持工具调用/耗时。"""
    import time
    t0 = time.monotonic()
    try:
        s = load_settings(payload)
        if s.needs_key and not s.key:
            raise ProviderError(s.key_error or "还没有填 API Key")
        client = client_for(s)
        messages = [
            {"role": "system", "content": "这是接口测试。请只按用户要求调用工具，不要闲聊。"},
            {"role": "user", "content": "请调用 xg_ping 工具，参数 text 固定为 pong。"},
        ]
        first = client.chat(messages, tools=[TEST_TOOL], max_tokens=200, temperature=0.1, timeout=60)
        calls = first.get("tool_calls") or []
        tool_ok = any(((c.get("function") or {}).get("name") == "xg_ping") for c in calls if isinstance(c, dict))
        reply = (first.get("content") or "").strip()
        if tool_ok:
            call = next(c for c in calls if ((c.get("function") or {}).get("name") == "xg_ping"))
            tool_id = call.get("id") or "call_test"
            messages.append({"role": "assistant", "content": first.get("content") or "", "tool_calls": calls})
            messages.append({"role": "tool", "tool_call_id": tool_id, "content": "pong"})
            second = client.chat(messages, tools=[TEST_TOOL], max_tokens=200, temperature=0.1, timeout=60)
            reply = (second.get("content") or reply or "").strip()
        elapsed = int((time.monotonic() - t0) * 1000)
        return {
            "ok": True, "connected": True, "tool_support": bool(tool_ok),
            "reply": _scrub(reply, s.key)[:600], "elapsed_ms": elapsed,
            "provider": s.provider, "name": s.name, "model": s.model,
        }
    except ProviderError as exc:
        return {"ok": False, "connected": False, "tool_support": False,
                "error": _scrub(exc.message, ""), "elapsed_ms": int((time.monotonic() - t0) * 1000),
                "status": exc.status, "kind": exc.kind}
    except ai_secrets.SecretError as exc:
        return {"ok": False, "connected": False, "tool_support": False, "error": str(exc),
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}
    except Exception as exc:
        return {"ok": False, "connected": False, "tool_support": False,
                "error": _scrub(f"测试失败：{type(exc).__name__}"),
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}


def load_environment() -> AiSettings:
    """兼容 ai_companion.load_secrets：把 secrets.env 读进环境并返回当前设置。"""
    _load_secrets_env()
    return load_settings()


def config_error(settings: AiSettings) -> str:
    if not settings.model:
        return "还没有选 AI 模型：打开网页顶部的“设置 → AI 接口”，拉模型列表或手填模型名。"
    if settings.needs_key and not settings.key:
        return settings.key_error or "还没有填 AI API Key：打开网页顶部的“设置 → AI 接口”填一个。"
    return "AI 接口还没有配置好：打开网页顶部的“设置 → AI 接口”。"
