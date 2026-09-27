"""Configurable LLM backends for Nextcloud Hybrid RAG.

Supported backends:
- ollama: native Ollama /api/chat API, including think and JSON format/schema.
- openai: OpenAI-compatible /v1/chat/completions API.

Authentication is service-to-service.  When api_key is set, every backend request
contains:

    Authorization: Bearer <api_key>

This also works for a self-hosted Ollama instance placed behind nginx/Caddy/etc.
The local Ollama API itself does not provide an authentication layer.
"""

from __future__ import annotations

import json
import ssl
from dataclasses import dataclass
from typing import Any, AsyncIterator, ClassVar
from urllib.parse import urlparse

import httpx

from rag.policy_hooks import PRE_MODEL_EGRESS, apply_policy_hook
from rag.network_policy import pinned_private_target


def _verify_value(verify_tls: bool, ca_file: str | None) -> bool | ssl.SSLContext:
    if not verify_tls:
        return False
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    return True


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") in {"text", "output_text"}:
                text = item.get("text")
                if text:
                    parts.append(str(text))
        return "".join(parts)
    return str(content or "")


@dataclass
class LLMBackend:
    base_url: str
    model: str
    api_key: str = ""
    verify_tls: bool = True
    ca_file: str | None = None
    private_network_only: bool = False
    trust_env: bool = True

    kind: ClassVar[str] = "base"

    def __post_init__(self) -> None:
        self.base_url = str(self.base_url).rstrip("/")
        self.model = str(self.model)
        self.api_key = str(self.api_key or "")
        self._verify = _verify_value(self.verify_tls, self.ca_file)

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request_target(
        self,
        path: str,
    ) -> tuple[str, dict[str, str], dict[str, str] | None]:
        url = f"{self.base_url}{path}"
        if not self.private_network_only:
            return url, {}, None
        pinned_url, host_header, extensions = pinned_private_target(url)
        return pinned_url, {"Host": host_header}, extensions

    def _policy_messages(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
    ) -> list[dict[str, str]]:
        checked = apply_policy_hook(
            PRE_MODEL_EGRESS,
            content=messages,
            metadata={
                "kind": "llm",
                "backend": self.kind,
                "base_url": self.base_url,
                "model": model,
            },
        )
        if not isinstance(checked, list) or any(
            not isinstance(item, dict) for item in checked
        ):
            raise TypeError("pre_model_egress policy hook must return a message list")
        return checked

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        options: dict[str, Any],
        think: bool | str | None = None,
        model: str | None = None,
        response_format: str | dict[str, Any] | None = None,
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        options: dict[str, Any],
        think: bool | str | None = None,
        model: str | None = None,
        read_timeout: float | None = 90.0,
    ) -> AsyncIterator[dict[str, str]]:
        raise NotImplementedError

    def info(self) -> dict[str, Any]:
        return {
            "backend": self.kind,
            "base_url": self.base_url,
            "model": self.model,
            "authenticated": bool(self.api_key),
            "verify_tls": bool(self.verify_tls),
            "ca_file": bool(self.ca_file),
            "private_network_only": bool(self.private_network_only),
            "trust_env": bool(self.trust_env),
        }


class OllamaBackend(LLMBackend):
    kind = "ollama"

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        options: dict[str, Any],
        think: bool | str | None = None,
        model: str | None = None,
        response_format: str | dict[str, Any] | None = None,
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        selected_model = model or self.model
        messages = self._policy_messages(messages, model=selected_model)
        payload: dict[str, Any] = {
            "model": selected_model,
            "messages": messages,
            "stream": False,
            "options": dict(options),
        }
        if think is not None:
            payload["think"] = think
        if response_format is not None:
            payload["format"] = response_format

        target_url, target_headers, target_extensions = self._request_target("/api/chat")
        async with httpx.AsyncClient(
            timeout=timeout,
            headers=self.headers(),
            verify=self._verify,
            trust_env=self.trust_env,
        ) as client:
            response = await client.post(
                target_url,
                json=payload,
                headers=target_headers or None,
                extensions=target_extensions,
            )
            response.raise_for_status()
            data = response.json()

        message = data.get("message") or {}
        return {
            "content": _content_to_text(message.get("content")).strip(),
            "thinking": str(message.get("thinking") or ""),
            "done_reason": data.get("done_reason"),
            "eval_count": data.get("eval_count"),
            "raw": data,
        }

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        options: dict[str, Any],
        think: bool | str | None = None,
        model: str | None = None,
        read_timeout: float | None = 90.0,
    ) -> AsyncIterator[dict[str, str]]:
        selected_model = model or self.model
        messages = self._policy_messages(messages, model=selected_model)
        payload: dict[str, Any] = {
            "model": selected_model,
            "messages": messages,
            "stream": True,
            "options": dict(options),
        }
        if think is not None:
            payload["think"] = think

        timeout = httpx.Timeout(
            connect=30.0,
            read=read_timeout,
            write=30.0,
            pool=30.0,
        )

        target_url, target_headers, target_extensions = self._request_target("/api/chat")
        async with httpx.AsyncClient(
            timeout=timeout,
            headers=self.headers(),
            verify=self._verify,
            trust_env=self.trust_env,
        ) as client:
            async with client.stream(
                "POST",
                target_url,
                json=payload,
                headers=target_headers or None,
                extensions=target_extensions,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    message = data.get("message") or {}
                    content = str(message.get("content") or "")
                    reasoning = str(message.get("thinking") or "")
                    if content or reasoning:
                        yield {
                            "content": content,
                            "reasoning": reasoning,
                        }
                    if data.get("done"):
                        break


class OpenAICompatibleBackend(LLMBackend):
    kind = "openai"

    def _is_native_openai(self) -> bool:
        """True only for OpenAI's public API, not generic compatible servers."""
        try:
            return (urlparse(self.base_url).hostname or "").casefold() == "api.openai.com"
        except Exception:
            return False

    @staticmethod
    def _is_gpt56(model: str) -> bool:
        return str(model or "").casefold().startswith("gpt-5.6")

    def _reasoning_effort(self, think: bool | str | None, model: str) -> str | None:
        """Map the provider's generic think knob to native GPT-5.6 semantics."""
        if self._is_native_openai() and self._is_gpt56(model):
            if think is False:
                return "none"
            if think is True:
                return "medium"
            if isinstance(think, str):
                value = think.strip().casefold()
                if value in {"none", "low", "medium", "high", "xhigh", "max"}:
                    return value
            return None
        if isinstance(think, str) and think in {"low", "medium", "high"}:
            return think
        return None

    def _payload_options(
        self,
        options: dict[str, Any],
        *,
        model: str,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        """Translate the useful common subset of Ollama-style options."""
        result: dict[str, Any] = {}
        native_gpt56 = self._is_native_openai() and self._is_gpt56(model)
        mapping = {
            "temperature": "temperature",
            "top_p": "top_p",
            "seed": "seed",
            "stop": "stop",
            # OpenAI's current Chat Completions contract uses
            # max_completion_tokens. Keep max_tokens for third-party compatible
            # servers so this backend remains portable.
            "num_predict": "max_completion_tokens" if self._is_native_openai() else "max_tokens",
        }
        for source, target in mapping.items():
            if source in options and options[source] is not None:
                # Native GPT-5.6 does not need sampling controls for these
                # planner/verifier/answer calls. Omitting them avoids model-
                # specific compatibility traps across reasoning efforts.
                if native_gpt56 and source in {"temperature", "top_p"}:
                    continue
                result[target] = options[source]
        # top_k, min_p, repeat_penalty and num_ctx are intentionally not sent:
        # they are not part of the portable OpenAI chat-completions contract.
        return result

    @staticmethod
    def _response_format(
        response_format: str | dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if response_format is None:
            return None
        if response_format == "json":
            return {"type": "json_object"}
        if isinstance(response_format, dict):
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": "rag_control",
                    "strict": True,
                    "schema": response_format,
                },
            }
        return None

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        options: dict[str, Any],
        think: bool | str | None = None,
        model: str | None = None,
        response_format: str | dict[str, Any] | None = None,
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        selected_model = model or self.model
        messages = self._policy_messages(messages, model=selected_model)
        reasoning_effort = self._reasoning_effort(think, selected_model)
        payload: dict[str, Any] = {
            "model": selected_model,
            "messages": messages,
            "stream": False,
            **self._payload_options(
                options, model=selected_model, reasoning_effort=reasoning_effort
            ),
        }

        fmt = self._response_format(response_format)

        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort

        # "OpenAI-compatible" providers differ most around structured output.
        # Prefer the strongest requested contract, then degrade deterministically
        # to json_object and finally prompt-only JSON if the provider rejects
        # response_format itself. Other HTTP errors are never hidden.
        formats: list[dict[str, Any] | None] = [fmt]
        if isinstance(fmt, dict) and fmt.get("type") == "json_schema":
            formats.append({"type": "json_object"})
        if fmt is not None:
            formats.append(None)

        # Preserve order while removing duplicates.
        unique_formats: list[dict[str, Any] | None] = []
        for candidate in formats:
            if candidate not in unique_formats:
                unique_formats.append(candidate)

        target_url, target_headers, target_extensions = self._request_target("/chat/completions")
        async with httpx.AsyncClient(
            timeout=timeout,
            headers=self.headers(),
            verify=self._verify,
            trust_env=self.trust_env,
        ) as client:
            response = None
            for idx, candidate in enumerate(unique_formats):
                attempt = dict(payload)
                if candidate is not None:
                    attempt["response_format"] = candidate
                response = await client.post(
                    target_url,
                    json=attempt,
                    headers=target_headers or None,
                    extensions=target_extensions,
                )
                if response.is_success:
                    break
                can_fallback = (
                    idx + 1 < len(unique_formats)
                    and response.status_code in {400, 404, 415, 422}
                )
                if not can_fallback:
                    response.raise_for_status()

            assert response is not None
            response.raise_for_status()
            data = response.json()

        choices = data.get("choices") or []
        message = choices[0].get("message") if choices else {}
        message = message or {}
        content = _content_to_text(message.get("content")).strip()
        thinking = str(
            message.get("reasoning_content")
            or message.get("reasoning")
            or ""
        )
        return {
            "content": content,
            "thinking": thinking,
            "done_reason": choices[0].get("finish_reason") if choices else None,
            "eval_count": (data.get("usage") or {}).get("completion_tokens"),
            "raw": data,
        }

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        options: dict[str, Any],
        think: bool | str | None = None,
        model: str | None = None,
        read_timeout: float | None = 90.0,
    ) -> AsyncIterator[dict[str, str]]:
        selected_model = model or self.model
        messages = self._policy_messages(messages, model=selected_model)
        reasoning_effort = self._reasoning_effort(think, selected_model)
        payload: dict[str, Any] = {
            "model": selected_model,
            "messages": messages,
            "stream": True,
            **self._payload_options(
                options, model=selected_model, reasoning_effort=reasoning_effort
            ),
        }
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort

        timeout = httpx.Timeout(
            connect=30.0,
            read=read_timeout,
            write=30.0,
            pool=30.0,
        )

        target_url, target_headers, target_extensions = self._request_target("/chat/completions")
        async with httpx.AsyncClient(
            timeout=timeout,
            headers=self.headers(),
            verify=self._verify,
            trust_env=self.trust_env,
        ) as client:
            async with client.stream(
                "POST",
                target_url,
                json=payload,
                headers=target_headers or None,
                extensions=target_extensions,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if line == "[DONE]":
                        break
                    data = json.loads(line)
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = _content_to_text(delta.get("content"))
                    reasoning = _content_to_text(
                        delta.get("reasoning_content")
                        or delta.get("reasoning")
                        or delta.get("thinking")
                    )
                    if content or reasoning:
                        yield {
                            "content": content,
                            "reasoning": reasoning,
                        }


def build_llm_backend(
    backend: str,
    *,
    base_url: str,
    model: str,
    api_key: str = "",
    verify_tls: bool = True,
    ca_file: str | None = None,
    private_network_only: bool = False,
    trust_env: bool = True,
) -> LLMBackend:
    name = str(backend or "ollama").strip().lower()
    kwargs = {
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "verify_tls": verify_tls,
        "ca_file": ca_file,
        "private_network_only": private_network_only,
        "trust_env": trust_env,
    }
    if name in {"ollama", "native_ollama"}:
        return OllamaBackend(**kwargs)
    if name in {"openai", "openai_compatible", "openai-compatible"}:
        return OpenAICompatibleBackend(**kwargs)
    raise RuntimeError(
        "LLM_BACKEND muss 'ollama' oder 'openai' sein "
        f"(erhalten: {backend!r})"
    )
