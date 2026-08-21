# services/llm_service/models/llm_models.py

"""Model identifiers supported by the LLM service."""

from enum import StrEnum


class OpenAIModel(StrEnum):
    """Supported OpenAI models."""

    GPT_5 = "gpt-5"
    GPT_5_MINI = "gpt-5-mini"
    GPT_5_NANO = "gpt-5-nano"


class ClaudeModel(StrEnum):
    """Supported Anthropic Claude models."""

    CLAUDE_SONNET_4 = "claude-sonnet-4-20250514"
    CLAUDE_OPUS_4 = "claude-opus-4-20250514"