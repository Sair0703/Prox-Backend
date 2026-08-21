# services/llm_service/client_factory.py

"""Creates configured SDK clients for supported LLM providers."""

from anthropic import Anthropic
from openai import OpenAI


def create_openai_client(api_key: str) -> OpenAI:
    """
    Create an authenticated OpenAI client.

    :param api_key: OpenAI API key.
    :return: Configured OpenAI client.
    """
    return OpenAI(api_key=api_key)


def create_claude_client(api_key: str) -> Anthropic:
    """
    Create an authenticated Anthropic client.

    :param api_key: Anthropic API key.
    :return: Configured Anthropic client.
    """
    return Anthropic(api_key=api_key)