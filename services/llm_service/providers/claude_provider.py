# services/llm_service/providers/claude_provider.py

"""Anthropic adapter implementing the common LLM provider interface."""

from __future__ import annotations

import logging
from typing import Any

from services.llm_service.models.base_models import LLMRequest, LLMResponse, LLMUsage
from services.llm_service.providers.llm_provider import LLMProvider

logger = logging.getLogger(__name__)


class ClaudeProvider(LLMProvider):
    """Adapts the Anthropic client to the common LLM provider contract."""

    def __init__(self, client: Any, default_model: str | None = None) -> None:
        """
        Initialize the Anthropic provider.

        :param client: Configured Anthropic client used to execute requests.
        :param default_model: Model used when a request does not specify one.
        """
        self.client = client
        self.default_model = default_model

    def generate(self, request: LLMRequest) -> LLMResponse:
        """
        Execute an Anthropic request and normalize its response.

        :param request: LLM request containing the prompt and generation settings.
        :return: Normalized response containing generated text and usage metadata.
        """
        model = request.model or self.default_model
        if not model:
            return LLMResponse(
                success=False,
                provider="anthropic",
                model="",
                raw_text="",
                error="ClaudeProvider requires a model.",
            )

        try:
            response = self.client.messages.create(
                model=model,
                max_tokens=request.max_tokens or 1024,
                temperature=request.temperature,
                messages=[
                    {
                        "role": "user",
                        "content": request.prompt,
                    }
                ],
            )

            raw_text = self._extract_text(response)
            usage = self._extract_usage(response)

            return LLMResponse(
                success=True,
                provider="anthropic",
                model=model,
                raw_text=raw_text,
                parsed_output=None,
                error=None,
                usage=usage,
                metadata={
                    "task_name": request.task_name,
                    "response_id": getattr(response, "id", None),
                },
            )

        except Exception as exc:
            logger.exception(
                "[CLAUDE_PROVIDER] generate failed task=%s",
                request.task_name,
            )
            return LLMResponse(
                success=False,
                provider="anthropic",
                model=model,
                raw_text="",
                parsed_output=None,
                error=str(exc),
                usage=None,
                metadata={"task_name": request.task_name},
            )

    @staticmethod
    def _extract_text(response: Any) -> str:
        """
        Extract generated text from an Anthropic response.

        :param response: Raw response returned by the Anthropic client.
        :return: Extracted generated text.
        :raises ValueError: If the response contains no extractable text.
        """
        content = getattr(response, "content", None)
        if content:
            chunks: list[str] = []
            for block in content:
                block_type = getattr(block, "type", None)
                text = getattr(block, "text", None)
                if block_type == "text" and text:
                    chunks.append(str(text))
            if chunks:
                return "".join(chunks).strip()

        raise ValueError("Claude response did not contain extractable text.")

    @staticmethod
    def _extract_usage(response: Any) -> LLMUsage | None:
        """
        Extract token usage from an Anthropic response.

        :param response: Raw response returned by the Anthropic client.
        :return: Normalized token usage, or None when usage is unavailable.
        """
        usage = getattr(response, "usage", None)
        if usage is None:
            return None

        return LLMUsage(
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
        )