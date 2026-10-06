"""Translate words picked in the subtitles, with a local LLM served by Ollama.

    POST /translate {"token": <LiveKit token>, "text": <the words>, "context": <their subtitle line>,
                     "target": "zh-TW"}
        -> {"translation": "...", "target": "zh-TW"}, or {"error": "..."} with a 4xx/5xx status

The subtitle line goes along so the model picks the meaning the words have
there. Only room participants (a valid token) may ask. Settings:
``OLLAMA_URL`` (default http://127.0.0.1:11434, or the Docker host's
http://host.docker.internal:11434 inside a container) and ``OLLAMA_MODEL``
(default qwen2.5:7b).
"""

from __future__ import annotations

import collections
import os
import re
from pathlib import Path

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from reflex_ddns_livekit_english_chat import livekit_server

# In a container, 127.0.0.1 is the container itself: Ollama runs on the Docker host.
_OLLAMA_HOST = "host.docker.internal" if Path("/.dockerenv").exists() else "127.0.0.1"
OLLAMA_URL = (os.environ.get("OLLAMA_URL", "").strip() or f"http://{_OLLAMA_HOST}:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "").strip() or "qwen2.5:7b"

# (code, how the picker shows it, how the model is told)
LANGUAGES: list[tuple[str, str, str]] = [
    ("zh-TW", "繁體中文", "Traditional Chinese as written in Taiwan (繁體中文)"),
    ("zh-CN", "简体中文", "Simplified Chinese (简体中文)"),
    ("ja", "日本語", "Japanese"),
    ("ko", "한국어", "Korean"),
    ("vi", "Tiếng Việt", "Vietnamese"),
    ("th", "ไทย", "Thai"),
    ("id", "Bahasa Indonesia", "Indonesian"),
    ("ms", "Bahasa Melayu", "Malay"),
    ("fil", "Filipino", "Filipino (Tagalog)"),
    ("hi", "हिन्दी", "Hindi"),
    ("ar", "العربية", "Arabic"),
    ("es", "Español", "Spanish"),
    ("pt", "Português", "Portuguese"),
    ("fr", "Français", "French"),
    ("de", "Deutsch", "German"),
    ("it", "Italiano", "Italian"),
    ("ru", "Русский", "Russian"),
    ("uk", "Українська", "Ukrainian"),
    ("tr", "Türkçe", "Turkish"),
    ("pl", "Polski", "Polish"),
    ("nl", "Nederlands", "Dutch"),
]
DEFAULT_LANGUAGE = "zh-TW"
_NAMES = {code: name for code, _label, name in LANGUAGES}
# Written in another script than English: a romanization the model adds is noise.
_NON_LATIN = {"zh-TW", "zh-CN", "ja", "ko", "th", "hi", "ar", "ru", "uk"}

_SYSTEM = (
    "You help someone follow an English conversation. Translate the English text marked SELECTED "
    "into {language}. CONTEXT is the sentence it was said in: use it to choose the meaning the "
    "words have there, but translate only SELECTED. Answer with the translation alone: no quotes, "
    "no notes, no romanization, no English."
)
_EXTRA = {"zh-TW": " Write Traditional Chinese characters as used in Taiwan, never Simplified ones."}
_CACHE_SIZE = 500
_cache: collections.OrderedDict[tuple[str, str, str], str] = collections.OrderedDict()
_to_taiwan = None


class TranslateError(Exception):
    pass


def _clean(answer: str, target: str) -> str:
    global _to_taiwan
    # Reasoning models think aloud first; quotes are often added despite the instructions.
    answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.S).strip()
    if len(answer) >= 2 and answer[0] == answer[-1] and answer[0] in "\"'「『“":
        answer = answer[1:-1].strip()
    if target in _NON_LATIN:
        # e.g. "銀行 (yínháng)"
        answer = re.sub(r"\s*[(（][A-Za-zÀ-ɏ̀-ͯ\s'.,-]+[)）]$", "", answer).strip()
    if target == "zh-TW":
        # Chinese models drift into Simplified characters; convert what slipped through.
        if _to_taiwan is None:
            from opencc import OpenCC

            _to_taiwan = OpenCC("s2twp")
        answer = _to_taiwan.convert(answer)
    return answer


async def translate(text: str, context: str, target: str) -> str:
    key = (text, context, target)
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM.format(language=_NAMES[target]) + _EXTRA.get(target, "")},
            {"role": "user", "content": f"CONTEXT: {context or text}\nSELECTED: {text}"},
        ],
        "stream": False,
        "options": {"temperature": 0.1},
        "keep_alive": "30m",
    }
    try:
        # The first request also loads the model into memory.
        async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
            resp = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
    except httpx.HTTPError as exc:
        msg = f"Can't reach the translator (Ollama at {OLLAMA_URL}). Is Ollama running? ({exc})"
        raise TranslateError(msg) from exc
    if resp.status_code == 404 and "not found" in resp.text:
        msg = f"The translator model {OLLAMA_MODEL} is not installed. Run: ollama pull {OLLAMA_MODEL}"
        raise TranslateError(msg)
    if resp.status_code != 200:
        msg = f"The translator failed ({resp.status_code}): {resp.text[:200]}"
        raise TranslateError(msg)
    answer = _clean(str(resp.json().get("message", {}).get("content", "")), target)
    if not answer:
        msg = "The translator gave no answer."
        raise TranslateError(msg)
    _cache[key] = answer
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return answer


async def _translate_endpoint(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "Bad request."}, status_code=400)
    if not isinstance(body, dict) or livekit_server.verify_token(str(body.get("token", ""))) is None:
        return JSONResponse({"error": "Join a room to translate."}, status_code=401)
    text = " ".join(str(body.get("text", "")).split())[:500]
    context = " ".join(str(body.get("context", "")).split())[:1000]
    target = str(body.get("target", ""))
    if target not in _NAMES:
        target = DEFAULT_LANGUAGE
    if not text:
        return JSONResponse({"error": "Nothing to translate."}, status_code=400)
    try:
        translation = await translate(text, context, target)
    except TranslateError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"translation": translation, "target": target})


def register_routes(app) -> None:  # noqa: ANN001 - Reflex App
    route = Route("/translate", _translate_endpoint, methods=["POST"])
    if all(getattr(r, "path", None) != route.path for r in app._api.routes):
        app._api.routes.append(route)
