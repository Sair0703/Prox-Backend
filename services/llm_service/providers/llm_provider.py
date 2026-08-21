# services/llm_service/providers/llm_provider.py

"""Common interface for LLM provider implementations."""

from __future__ import annotations

from abc import ABC, abstractmethod

from services.llm_service.models.base_models import LLMRequest, LLMResponse


class LLMProvider(ABC):
    """Defines the provider-independent interface for LLM execution."""

    @abstractmethod
    def generate(self, request: LLMRequest) -> LLMResponse:
        """
        Execute an LLM request through the provider.

        :param request: LLM request containing the prompt and generation settings.
        :return: Normalized response returned by the provider.
        """
        raise NotImplementedError