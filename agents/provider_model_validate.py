"""Provider vs model id consistency (Anthropic / DeepSeek / OpenRouter).

Keep in sync with web/manager/provider_model_validate.py
"""


def validate_provider_model(
    provider: str,
    model: str,
    *,
    field_label: str = "",
) -> None:
    """Raise ValueError if ``model`` does not match the selected ``provider``."""
    p = (provider or "").strip().lower()
    m_raw = (model or "").strip()
    label = f"{field_label}: " if field_label else ""

    if not m_raw:
        raise ValueError(f"{label}Model is required")

    low = m_raw.lower()

    if p == "anthropic":
        if not low.startswith("claude"):
            raise ValueError(
                f'{label}Anthropic expects a Claude model id starting with "claude" '
                f"(e.g. claude-sonnet-4-20250514); got {m_raw!r}"
            )
    elif p == "deepseek":
        if not low.startswith("deepseek"):
            raise ValueError(
                f'{label}DeepSeek expects a model id starting with "deepseek" '
                f"(e.g. deepseek-chat); got {m_raw!r}"
            )
    elif p == "openrouter":
        if not low.startswith("openrouter") and "/" not in m_raw:
            raise ValueError(
                f'{label}OpenRouter expects a vendor/model slug containing "/" '
                f"(e.g. anthropic/claude-3.5-sonnet, deepseek/deepseek-chat) "
                f'or a model id starting with "openrouter"; got {m_raw!r}'
            )
    else:
        raise ValueError(f"{label}Unknown provider: {provider!r}")
