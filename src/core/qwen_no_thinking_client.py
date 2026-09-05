"""Custom OpenAI client for Qwen models with thinking mode disabled.

Qwen3 series models have thinking mode enabled by default, generating
large amounts of reasoning tokens (1000+ per call) before producing output.
This causes 10+ second latency per LLM call.

This client disables thinking mode via extra_body parameter.
"""

from __future__ import annotations

from typing import Any

from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.llm_client.config import LLMConfig, ModelSize
from graphiti_core.prompts.models import Message
from pydantic import BaseModel


class QwenNoThinkingClient(OpenAIGenericClient):
    """OpenAI-compatible client with Qwen thinking mode disabled."""

    def __init__(
        self,
        config: LLMConfig | None = None,
        cache: bool = False,
        max_tokens: int | None = None,
        structured_output_mode: str | None = None,
    ):
        # Initialize parent class — use keyword args to avoid positional mismatch
        # Parent signature: (config, cache, client, max_tokens, structured_output_mode)
        super().__init__(config=config, cache=cache, max_tokens=max_tokens, structured_output_mode=structured_output_mode)

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = 16384,
        model_size: ModelSize = ModelSize.medium,
    ) -> dict[str, Any]:
        """Generate response with thinking mode disabled.
        
        This overrides the parent method to add extra_body={'enable_thinking': False}
        to the API call, which suppresses Qwen's reasoning token generation.
        
        We directly call the API instead of monkey-patching to avoid recursion
        issues with tenacity retries in the parent class.
        """
        import json
        import logging
        import openai
        from openai.types.chat import ChatCompletionMessageParam
        from graphiti_core.llm_client.errors import RateLimitError, EmptyResponseError
        
        logger = logging.getLogger(__name__)
        
        # Convert messages to OpenAI format (same as parent)
        openai_messages: list[ChatCompletionMessageParam] = []
        for m in messages:
            m.content = self._clean_input(m.content)
            if m.role == 'user':
                openai_messages.append({'role': 'user', 'content': m.content})
            elif m.role == 'system':
                openai_messages.append({'role': 'system', 'content': m.content})
        
        try:
            response = await self.client.chat.completions.create(
                model=self.model or 'qwen3-flash',
                messages=openai_messages,
                temperature=self.temperature,
                max_tokens=max_tokens,
                response_format=self._build_response_format(response_model),
                extra_body={'enable_thinking': False},  # Disable Qwen thinking mode
            )
            result = response.choices[0].message.content or ''
            if not result:
                raise EmptyResponseError('LLM returned an empty response')
            return json.loads(self._strip_code_fences(result))
        except openai.RateLimitError as e:
            raise RateLimitError from e
        except Exception as e:
            logger.error(f'Error in generating LLM response: {e}')
            raise
