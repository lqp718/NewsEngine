"""Graphiti SDK client management - provides a factory function for Graphiti instances.

Since Graphiti SDK's add_episode() method is not thread-safe, we use a factory function
rather than a singleton to allow each caller to manage their own instance lifecycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from graphiti_core import Graphiti
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.llm_client.client import LLMConfig, Message, ModelSize
from graphiti_core.llm_client.errors import EmptyResponseError
from graphiti_core.cross_encoder.bge_reranker_client import BGERerankerClient

import json
import logging
from pydantic import BaseModel
from typing import Union, Dict, List, Type
from collections.abc import Mapping
from functools import lru_cache

# JSON Schema meta keys that indicate a schema echo rather than instance data
_SCHEMA_META_KEYS = frozenset({
    "properties", "type", "title", "description",
    "$defs", "required", "additionalProperties",
    "items", "anyOf", "oneOf", "allOf", "enum",
    "default", "examples",
})


def _detect_schema_echo(result: dict, response_model: type[BaseModel]) -> bool:
    """Detect if LLM returned JSON Schema structure instead of instance data.
    
    Args:
        result: Parsed dict from LLM response
        response_model: Expected Pydantic model class
        
    Returns:
        True if result appears to be schema echo, False otherwise
    """
    result_keys = set(result.keys())
    schema_meta_hits = result_keys & _SCHEMA_META_KEYS
    
    # If we have at least 2 schema meta keys and no business fields, it's likely a schema echo
    if len(schema_meta_hits) < 2:
        return False
    
    # Check if response contains business fields from the expected model
    business_keys = set(response_model.model_json_schema().get("properties", {}).keys())
    has_business_data = bool(result_keys & business_keys)
    
    return not has_business_data


def _schema_type_to_prose(field_schema: dict) -> str:
    """Convert JSON Schema type declaration to prose description."""
    type_val = field_schema.get("type", "any")
    if isinstance(type_val, list):
        types = [_type_name(t) for t in type_val]
        return " or ".join(types)
    
    if type_val == "array":
        items = field_schema.get("items", {})
        item_type = _type_name(items.get("type", "any"))
        return f"array of {item_type}"
    
    if type_val == "object":
        return "object"
    
    return _type_name(type_val)


def _type_name(t: str) -> str:
    """Helper to convert type names to plural form."""
    mapping = {
        "integer": "integers", 
        "string": "strings", 
        "number": "numbers", 
        "boolean": "booleans"
    }
    return mapping.get(t, t)


def _build_prose_format(response_model: type[BaseModel]) -> str:
    """Build prose format description from Pydantic model.
    
    Args:
        response_model: Pydantic model class
        
    Returns:
        String describing expected JSON structure in prose
    """
    schema = response_model.model_json_schema()
    lines = ["Respond with a JSON object containing:"]
    
    for field_name, field_schema in schema.get("properties", {}).items():
        type_str = _schema_type_to_prose(field_schema)
        desc = field_schema.get("description", "")
        if desc:
            lines.append(f"  - {field_name}: {type_str} ({desc})")
        else:
            lines.append(f"  - {field_name}: {type_str}")
    
    return "\n".join(lines)


class SchemaEchoSafeClient(OpenAIGenericClient):
    """LLM client optimized for OpenAI-compatible providers.
    
    Addresses two issues common with less-reliable providers:
    1. Replaces raw JSON Schema injection with prose format to reduce schema echo
    2. Adds schema echo detection and retry capability
    """
    
    async def generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int | None = None,
        model_size: ModelSize = ModelSize.medium,
        group_id: str | None = None,
        prompt_name: str | None = None,
        *,
        attribute_extraction: bool = False,
    ) -> dict[str, Any]:
        """Override generate_response to use prose format instead of raw schema."""
        from graphiti_core.llm_client.client import get_extraction_language_instruction
        
        self._apply_attribute_extraction_preamble(messages, attribute_extraction)
        if max_tokens is None:
            max_tokens = self.max_tokens

        # Add multilingual extraction instructions (must come before prose injection
        # to match SDK ordering and ensure language instruction is in the system message)
        messages[0].content += get_extraction_language_instruction(group_id)

        # In json_object fallback mode, replace raw JSON Schema with prose format
        # to reduce likelihood of schema echo
        if response_model is not None and self.structured_output_mode == 'json_object':
            prose_format = _build_prose_format(response_model)
            messages[-1].content += f'\n\n{prose_format}'

        # Wrap entire operation in tracing span
        with self.tracer.start_span('llm.generate') as span:
            attributes = {
                'llm.provider': 'openai',
                'model.size': model_size.value,
                'max_tokens': max_tokens,
            }
            if prompt_name:
                attributes['prompt.name'] = prompt_name
            span.add_attributes(attributes)

            try:
                # Delegate to the base tenacity wrapper so transient JSONDecodeError /
                # RateLimitError get backoff-retried (4 attempts)
                return await self._generate_response_with_retry(
                    messages, response_model, max_tokens=max_tokens, model_size=model_size
                )
            except Exception as e:
                span.set_status('error', str(e))
                span.record_exception(e)
                raise
    
    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = 4096,  # Safe default for Dashscope (SDK default 16384 may be too high)
        model_size: ModelSize = ModelSize.medium,
    ) -> dict[str, Any]:
        """Override _generate_response to add schema echo detection.
        Default max_tokens=4096 is a safe conservative value for Dashscope;
        the upstream SDK default (DEFAULT_MAX_TOKENS) is 16384."""
        result = await super()._generate_response(
            messages, response_model, max_tokens, model_size
        )
        
        # Schema echo detection: check if response is JSON Schema structure instead of instance
        if response_model is not None and isinstance(result, dict):
            if _detect_schema_echo(result, response_model):
                logging.getLogger(__name__).warning(
                    "Detected schema echo in LLM response (model=%s, structured_output_mode=%s). "
                    "Raising EmptyResponseError to trigger retry.",
                    self.model,
                    self.structured_output_mode,
                )
                raise EmptyResponseError(
                    "LLM returned JSON Schema structure instead of instance data"
                )
        
        return result

if TYPE_CHECKING:
    from graphiti_core.driver.driver import GraphDriver

from .config import get_settings
from .neo4j_client import get_neo4j_driver


def create_graphiti(graph_driver: GraphDriver | None = None) -> Graphiti:
    """Create a new Graphiti instance with the configured LLM and Embedder clients.

    Args:
        graph_driver: Optional GraphDriver instance. If provided, Graphiti will use this
            driver instead of creating its own from uri/user/password credentials.
            This allows callers to share a driver with existing connection pools.
            When None, Graphiti creates an internal driver using configured credentials.

    Returns:
        A new Graphiti instance configured with the current settings.
    """
    settings = get_settings()

    # Configure LLM client with DeepSeek API
    llm_config = LLMConfig(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
    )
    llm_client = SchemaEchoSafeClient(
        config=llm_config,
        structured_output_mode='json_object',  # Dashscope 不支持 json_schema constrained decoding
    )

    # Configure Embedder client with 百炼 API
    embedder_config = OpenAIEmbedderConfig(
        api_key=settings.bailian_api_key,
        base_url=settings.openai_base_url,
        embedding_model=settings.embedding_model,
    )
    embedder_client = OpenAIEmbedder(config=embedder_config)

    # Configure reranker: BGE local model (GPU available → sub-100ms)
    # No API key needed, no per-call cost
    reranker_client = BGERerankerClient()

    # Create and return Graphiti instance
    return Graphiti(
        uri=settings.neo4j_uri,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
        llm_client=llm_client,
        embedder=embedder_client,
        cross_encoder=reranker_client,
        graph_driver=graph_driver,
    )


__all__ = ["create_graphiti"]
