from __future__ import annotations


VALID_HISTORY_ROLES = {"user", "assistant"}


def make_user_content(display_name: str, content: str) -> str:
    name = " ".join(display_name.strip().split()) or "Discord user"
    body = content.strip()
    return f"{name}: {body}"


def normalize_history(raw_history: object) -> list[dict[str, str]]:
    if not isinstance(raw_history, list):
        return []

    normalized: list[dict[str, str]] = []
    for item in raw_history:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role in VALID_HISTORY_ROLES and isinstance(content, str) and content.strip():
            normalized.append({"role": role, "content": content.strip()})
    return normalized


def trim_history(
    raw_history: object,
    *,
    max_turns: int,
    char_budget: int,
) -> list[dict[str, str]]:
    history = normalize_history(raw_history)
    if max_turns <= 0 or char_budget <= 0:
        return []

    max_messages = max_turns * 2
    history = history[-max_messages:]

    while history and _history_chars(history) > char_budget:
        history.pop(0)

    return history


def build_ollama_messages(
    *,
    system_prompt: str,
    history: list[dict[str, str]],
    user_content: str,
) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": system_prompt.strip()}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_content.strip()})
    return messages


def _history_chars(history: list[dict[str, str]]) -> int:
    return sum(len(item["content"]) for item in history)
