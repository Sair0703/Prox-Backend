# LLM Service

The LLM Service provides a unified interface for executing LLM-backed tasks across backend services. It handles prompt rendering, provider execution, and structured response parsing behind a provider-independent interface.

## Features

* Unified LLM task execution through `LLMService`
* Task-specific prompt templates and input/output contracts
* Structured JSON response parsing
* OpenAI and Anthropic Claude provider support
* Extensible prompt and provider architecture

### Store Location Tasks

* `detect_remaining_issues` — verifies which candidate store-location issues still remain
* `repair_store` — repairs unresolved store-location issues using semantic reasoning

## Requirements

Configure the API key for the provider you intend to use:

```env
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
```

The corresponding provider SDK must also be installed.

LLM credentials are optional unless an LLM-backed workflow is used. API keys should be stored in the environment and must not be committed to source control.

Provider credentials are loaded through `config/settings.py`, while configured SDK clients are created through `config/llm_client_factory.py`.

## Usage

Create the provider client through the LLM client factory, initialize the provider, and pass it to `LLMService`.

```python
from config.llm_client_factory import get_openai_client
from services.llm_service.llm_service import LLMService
from services.llm_service.providers.openai_provider import OpenAIProvider

client = get_openai_client()
provider = OpenAIProvider(client=client)
llm_service = LLMService(provider=provider)

response = llm_service.execute(
    task_name="detect_remaining_issues",
    payload={
        "store_location": store_location,
        "candidate_issues": candidate_issues,
    },
    model="gpt-5",
)
```

`LLMService.execute()` returns an `LLMResponse` containing the raw model output, parsed JSON output when successful, provider usage metadata, and error information when execution or parsing fails.
