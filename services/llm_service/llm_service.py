# services/llm_service/llm_service.py

"""Unified service interface for executing task-specific LLM workflows."""

from __future__ import annotations

import json
from typing import Any

from services.llm_service.models.base_models import LLMRequest, LLMResponse, PromptSpec
from services.llm_service.models.prompt_specs import PROMPT_SPECS
from services.llm_service.prompts.prompt_renderer import PromptRenderer
from services.llm_service.providers.llm_provider import LLMProvider


class LLMService:
    """Provides a unified interface for task-specific LLM execution."""

    def __init__(
        self,
        provider: LLMProvider,
        renderer: PromptRenderer | None = None,
    ) -> None:
        """
        Initialize the LLM service.

        :param provider: LLM provider used to execute model requests.
        :param renderer: Prompt renderer used to build task-specific prompts.
        """
        self.provider = provider
        self.renderer = renderer or PromptRenderer()

    def execute(
        self,
        task_name: str,
        payload: dict[str, Any],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """
        Execute a registered LLM task using structured input data.

        :param task_name: Name of the registered prompt task to execute.
        :param payload: Structured input data required by the task.
        :param model: Model used to execute the request.
        :param temperature: Sampling temperature used for generation.
        :param max_tokens: Maximum number of output tokens, if specified.
        :return: Normalized response containing raw and parsed model output.
        :raises ValueError: If the requested task is not registered.
        """
        if task_name not in PROMPT_SPECS:
            raise ValueError(f"Unknown LLM task: {task_name}")

        spec = PROMPT_SPECS[task_name]
        rendered_prompt = self.renderer.render(spec, payload)

        request = LLMRequest(
            task_name=task_name,
            model=model,
            prompt=rendered_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        response = self.provider.generate(request)
        response = self._parse_response(response)

        return response

    @staticmethod
    def get_prompt_spec(task_name: str) -> PromptSpec:
        """
        Return the prompt specification registered for a task.

        :param task_name: Name of the registered prompt task.
        :return: Prompt specification associated with the task.
        :raises ValueError: If the requested task is not registered.
        """
        try:
            return PROMPT_SPECS[task_name]
        except KeyError as e:
            raise ValueError(f"Unknown prompt: {task_name}") from e

    @staticmethod
    def _parse_response(response: LLMResponse) -> LLMResponse:
        """
        Parse successful model output as structured JSON.

        :param response: Provider response containing the raw model output.
        :return: Response containing parsed output, or a failed response if parsing fails.
        """
        if not response.success:
            return response

        try:
            parsed = json.loads(response.raw_text)
        except Exception as e:
            return LLMResponse(
                success=False,
                provider=response.provider,
                model=response.model,
                raw_text=response.raw_text,
                parsed_output=None,
                error=f"JSON parse failed: {e}",
                usage=response.usage,
                metadata=response.metadata,
            )

        return LLMResponse(
            success=True,
            provider=response.provider,
            model=response.model,
            raw_text=response.raw_text,
            parsed_output=parsed,
            error=None,
            usage=response.usage,
            metadata=response.metadata,
        )