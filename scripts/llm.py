"""LLM プロバイダの薄いラッパー。

Gemini / OpenAI / Anthropic を同じインターフェースで呼べるようにして、
あとから API を差し替えても collect.py 側を触らずに済むようにする。
SDK を使わず HTTPS を直接叩くので、SDK のバージョン差で壊れない。
"""

from __future__ import annotations

import json
import os
import re
import time

import requests

TIMEOUT = 180


class LLMError(RuntimeError):
    pass


def _strip_code_fence(text: str) -> str:
    """```json ... ``` で包まれて返ってきた場合に中身だけ取り出す。"""
    text = text.strip()
    fence = re.match(r"^```[a-zA-Z]*\s*\n(.*?)\n?```$", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text


def _extract_json(text: str) -> dict:
    """モデルの出力から最初の JSON オブジェクトを取り出して parse する。"""
    text = _strip_code_fence(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 前後に説明文が付いてしまった場合の保険：最外の {...} を拾う
    start = text.find("{")
    if start == -1:
        raise LLMError(f"JSON が見つかりませんでした:\n{text[:600]}")
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError as exc:
                    raise LLMError(f"JSON の解析に失敗: {exc}\n{candidate[:600]}") from exc
    raise LLMError(f"JSON が閉じていません:\n{text[:600]}")


def _post_with_retry(url: str, *, headers: dict, payload: dict, provider: str) -> dict:
    last_error = None
    for attempt in range(4):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
        except requests.RequestException as exc:
            last_error = f"通信エラー: {exc}"
        else:
            if resp.status_code == 200:
                return resp.json()
            # レート制限・一時障害はリトライ
            if resp.status_code in (408, 409, 425, 429, 500, 502, 503, 504):
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            else:
                raise LLMError(f"{provider} API エラー HTTP {resp.status_code}: {resp.text[:600]}")
        wait = 4 * (2**attempt)
        print(f"    ! {provider} 呼び出し失敗（{last_error}）。{wait}秒待って再試行します")
        time.sleep(wait)
    raise LLMError(f"{provider} API に {4} 回失敗しました: {last_error}")


def call_json(system_prompt: str, user_prompt: str, cfg: dict) -> dict:
    """JSON を返させて dict にして戻す。"""
    provider = (cfg.get("provider") or "gemini").lower()
    if provider == "gemini":
        return _gemini(system_prompt, user_prompt, cfg)
    if provider == "openai":
        return _openai(system_prompt, user_prompt, cfg)
    if provider in ("anthropic", "claude"):
        return _anthropic(system_prompt, user_prompt, cfg)
    raise LLMError(f"未知のプロバイダです: {provider}（gemini / openai / anthropic のいずれか）")


def _require_key(name: str, provider: str) -> str:
    key = os.environ.get(name, "").strip()
    if not key:
        raise LLMError(
            f"{provider} を使う設定ですが、環境変数 {name} が空です。"
            f" .env に {name}=... を書いてください。"
        )
    return key


def _gemini(system_prompt: str, user_prompt: str, cfg: dict) -> dict:
    key = _require_key("GEMINI_API_KEY", "Gemini")
    model = cfg.get("gemini_model", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "maxOutputTokens": 16384,
        },
    }
    data = _post_with_retry(
        url,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        payload=payload,
        provider="Gemini",
    )
    try:
        candidate = data["candidates"][0]
        parts = candidate["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError) as exc:
        raise LLMError(f"Gemini の応答が想定外です: {json.dumps(data)[:600]}") from exc
    return _extract_json(text)


def _openai(system_prompt: str, user_prompt: str, cfg: dict) -> dict:
    key = _require_key("OPENAI_API_KEY", "OpenAI")
    model = cfg.get("openai_model", "gpt-4.1-mini")
    payload = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    data = _post_with_retry(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        payload=payload,
        provider="OpenAI",
    )
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise LLMError(f"OpenAI の応答が想定外です: {json.dumps(data)[:600]}") from exc
    return _extract_json(text)


def _anthropic(system_prompt: str, user_prompt: str, cfg: dict) -> dict:
    key = _require_key("ANTHROPIC_API_KEY", "Anthropic")
    model = cfg.get("anthropic_model", "claude-sonnet-4-5-20250929")
    payload = {
        "model": model,
        "max_tokens": 16384,
        "temperature": 0.2,
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": user_prompt},
            # 出力を必ず JSON から始めさせる
            {"role": "assistant", "content": "{"},
        ],
    }
    data = _post_with_retry(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        payload=payload,
        provider="Anthropic",
    )
    try:
        text = "".join(b.get("text", "") for b in data["content"])
    except (KeyError, TypeError) as exc:
        raise LLMError(f"Anthropic の応答が想定外です: {json.dumps(data)[:600]}") from exc
    return _extract_json("{" + text)
