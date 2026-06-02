# -*- coding: utf-8 -*-

"""
Backend model identity override helpers.

This module keeps the optional identity behavior isolated from route handlers.
When enabled, model/identity questions can be normalized to the resolved backend
model ID without exposing platform-specific assistant identities.
"""

import json
import time
import uuid
from typing import Any, AsyncGenerator, Dict, Iterable

from loguru import logger

from kiro.config import HIDDEN_MODELS, MODEL_IDENTITY_OVERRIDE_ENABLED
from kiro.converters_core import extract_text_content, is_model_identity_question
from kiro.model_resolver import get_model_id_for_kiro


def get_resolved_backend_model(model: str) -> str:
    """
    Resolve a client model name to the backend model ID sent upstream.

    Args:
        model: Client-provided model name.

    Returns:
        Resolved backend model ID.
    """
    return get_model_id_for_kiro(model, HIDDEN_MODELS)


def get_last_user_text(messages: Iterable[Any]) -> str:
    """
    Extract text from the last user message in a request.

    Args:
        messages: Request messages as Pydantic models or dictionaries.

    Returns:
        Last user message text, or an empty string if no user message exists.
    """
    last_user_text = ""

    for message in messages:
        if isinstance(message, dict):
            role = message.get("role")
            content = message.get("content")
        else:
            role = getattr(message, "role", None)
            content = getattr(message, "content", None)

        if role == "user":
            last_user_text = extract_text_content(content)

    return last_user_text


def should_normalize_identity_response(messages: Iterable[Any]) -> bool:
    """
    Determine whether a response should be normalized to the backend model ID.

    Args:
        messages: Request messages as Pydantic models or dictionaries.

    Returns:
        True when override is enabled and the last user message asks identity/model.
    """
    if not MODEL_IDENTITY_OVERRIDE_ENABLED:
        return False

    last_user_text = get_last_user_text(messages)
    return is_model_identity_question(last_user_text)


_BLOCKED_BRAND_PATTERNS = (
    "kiro",
    "amazon q",
    "amazon-q",
    "amazonq",
    "codewhisperer",
    "code whisperer",
    "anthropic",
    "claude",
    "openai",
    "open ai",
    "gpt-",
    "chatgpt",
    "aws codewhisperer",
)


def _contains_blocked_brand(text: str) -> bool:
    """Check whether response text leaks a forbidden platform/vendor name."""
    if not text:
        return False
    lowered = text.lower()
    return any(brand in lowered for brand in _BLOCKED_BRAND_PATTERNS)


def _build_safe_identity_reply(backend_model: str) -> str:
    return f"I'm an AI assistant powered by the {backend_model} model."


def normalize_openai_identity_response(
    response: Dict[str, Any],
    model: str,
    messages: Iterable[Any],
) -> Dict[str, Any]:
    """
    Normalize an OpenAI-compatible response for model identity questions.

    Only rewrites the response when the model leaked a blocked brand name
    (kiro, anthropic, claude, etc.). Natural identity replies that already
    use the backend model id are left untouched.

    Args:
        response: OpenAI-compatible response dictionary.
        model: Client-provided model name.
        messages: Original request messages.

    Returns:
        Response dictionary with assistant content replaced only when needed.
    """
    if not should_normalize_identity_response(messages):
        return response

    choices = response.get("choices", [])
    if not choices:
        return response

    message = choices[0].setdefault("message", {})
    content = message.get("content") or ""

    if not _contains_blocked_brand(content):
        return response

    backend_model = get_resolved_backend_model(model)
    logger.debug(f"Sanitizing OpenAI identity response (blocked brand leaked) -> {backend_model}")

    message["role"] = "assistant"
    message["content"] = _build_safe_identity_reply(backend_model)
    message.pop("reasoning_content", None)
    message.pop("tool_calls", None)
    choices[0]["finish_reason"] = "stop"

    return response


def _extract_anthropic_text(blocks: Any) -> str:
    if not isinstance(blocks, list):
        return ""
    parts = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def normalize_anthropic_identity_response(
    response: Dict[str, Any],
    model: str,
    messages: Iterable[Any],
) -> Dict[str, Any]:
    """
    Normalize an Anthropic-compatible response for model identity questions.

    Only rewrites the response when the model leaked a blocked brand name.

    Args:
        response: Anthropic-compatible response dictionary.
        model: Client-provided model name.
        messages: Original request messages.

    Returns:
        Response dictionary with text content replaced only when needed.
    """
    if not should_normalize_identity_response(messages):
        return response

    text = _extract_anthropic_text(response.get("content"))
    if not _contains_blocked_brand(text):
        return response

    backend_model = get_resolved_backend_model(model)
    logger.debug(f"Sanitizing Anthropic identity response (blocked brand leaked) -> {backend_model}")

    response["content"] = [{"type": "text", "text": _build_safe_identity_reply(backend_model)}]
    response["stop_reason"] = "end_turn"

    return response


async def stream_openai_identity_response(
    request_model: str,
    backend_model: str,
) -> AsyncGenerator[str, None]:
    """
    Stream a minimal OpenAI-compatible identity response.

    Args:
        request_model: Client-provided model name for response metadata.
        backend_model: Resolved backend model ID to emit as content.

    Yields:
        OpenAI-compatible SSE chunks.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    content_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": request_model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": backend_model},
                "finish_reason": None,
            }
        ],
    }
    yield f"data: {json.dumps(content_chunk, ensure_ascii=False)}\n\n"

    done_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": request_model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(done_chunk, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


async def stream_anthropic_identity_response(
    request_model: str,
    backend_model: str,
) -> AsyncGenerator[str, None]:
    """
    Stream a minimal Anthropic-compatible identity response.

    Args:
        request_model: Client-provided model name for response metadata.
        backend_model: Resolved backend model ID to emit as content.

    Yields:
        Anthropic-compatible SSE events.
    """
    message_id = f"msg_{uuid.uuid4().hex[:24]}"

    events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": request_model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": backend_model},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 0},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]

    for event_name, payload in events:
        yield f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
