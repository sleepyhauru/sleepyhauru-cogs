from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit


DEFAULT_OLLAMA_PORT = 11434


class OllamaClientError(Exception):
    """User-facing Ollama client failure."""


def normalize_base_url(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("Ollama URL cannot be empty.")

    if "://" not in raw:
        raw = f"http://{raw}"

    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Use an HTTP(S) URL or host, such as http://localhost:11434.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("The Ollama URL has an invalid port.") from exc

    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    if port is None:
        netloc = f"{host}:{DEFAULT_OLLAMA_PORT}"
    else:
        netloc = f"{host}:{port}"

    path = parsed.path.rstrip("/")
    if path in {"/api", "/api/chat"}:
        path = ""
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


@dataclass(slots=True)
class OllamaClient:
    base_url: str
    timeout_seconds: float = 90.0

    @property
    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/api/chat"

    @property
    def tags_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/api/tags"

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        data = await self._request_json("POST", self.chat_url, json=payload)
        message = data.get("message")
        if not isinstance(message, dict):
            raise OllamaClientError("Ollama returned a response without a message.")

        content = message.get("content")
        if not isinstance(content, str):
            raise OllamaClientError("Ollama returned a message without text content.")

        return content.strip()

    async def list_models(self) -> list[str]:
        data = await self._request_json("GET", self.tags_url)
        models = data.get("models", [])
        names: list[str] = []
        if isinstance(models, list):
            for model in models:
                if isinstance(model, dict) and isinstance(model.get("name"), str):
                    names.append(model["name"])
        return sorted(names)

    async def _request_json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            import aiohttp
        except ImportError as exc:
            raise OllamaClientError("aiohttp is required to talk to Ollama.") from exc

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, url, **kwargs) as response:
                    text = await response.text()
                    if response.status >= 400:
                        detail = _shorten_error(text)
                        if response.status == 404:
                            raise OllamaClientError(
                                f"Ollama returned 404. The model may not be installed, or the endpoint is unavailable. {detail}"
                            )
                        raise OllamaClientError(
                            f"Ollama returned HTTP {response.status}. {detail}"
                        )
                    try:
                        data = await response.json()
                    except aiohttp.ContentTypeError as exc:
                        raise OllamaClientError(
                            "Ollama did not return JSON. Check that the base URL points to the Ollama server."
                        ) from exc
        except TimeoutError as exc:
            raise OllamaClientError("Ollama took too long to respond.") from exc
        except aiohttp.ClientConnectorError as exc:
            raise OllamaClientError(
                "Could not connect to Ollama. Check the base URL and that Ollama is running."
            ) from exc
        except aiohttp.ClientError as exc:
            raise OllamaClientError(f"Ollama request failed: {exc}") from exc

        if not isinstance(data, dict):
            raise OllamaClientError("Ollama returned an unexpected JSON shape.")
        return data


def _shorten_error(text: str, limit: int = 350) -> str:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return ""
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3]}..."
