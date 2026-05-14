from __future__ import annotations


DISCORD_SAFE_MESSAGE_LIMIT = 1900


def split_discord_messages(
    text: str,
    *,
    limit: int = DISCORD_SAFE_MESSAGE_LIMIT,
) -> list[str]:
    cleaned = text.strip()
    if not cleaned:
        return ["Ollama returned an empty response."]

    chunks: list[str] = []
    remaining = cleaned
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = max(
            remaining.rfind("\n\n", 0, limit),
            remaining.rfind("\n", 0, limit),
            remaining.rfind(" ", 0, limit),
        )
        if split_at < int(limit * 0.45):
            split_at = limit

        chunk = remaining[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].strip()

    return chunks


def truncate_response(text: str, max_chars: int) -> str:
    cleaned = text.strip()
    if max_chars <= 0 or len(cleaned) <= max_chars:
        return cleaned
    suffix = "\n\n[Response truncated by the configured character limit.]"
    return f"{cleaned[: max_chars - len(suffix)].rstrip()}{suffix}"
