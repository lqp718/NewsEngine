"""百炼 OpenAI 兼容 LLM Client with Schema Echo 修复

百炼 qwen 在 json_object 模式下经常返回错误格式：
1. JSON Schema 定义而不是数据实例：{"properties": {...}, "type": "object"}
2. 返回 list 而不是 dict：[{...}] 而不是 {"edges": [...]}
3. 字段缺失或结构错误

这个 client 在 response 返回前进行后处理，修复这些格式问题。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic_core import PydanticUndefined

from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.prompts.models import Message
from graphiti_core.llm_client.config import ModelSize, DEFAULT_MAX_TOKENS

logger = logging.getLogger(__name__)


class BailianOpenAIClient(OpenAIGenericClient):
    """OpenAIGenericClient 子类，修复百炼 Schema Echo 和格式问题"""

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
    ) -> dict[str, Any]:
        """Override _generate_response to fix schema echo and format issues"""
        result = await super()._generate_response(
            messages, response_model, max_tokens, model_size
        )

        # 如果没有 response_model，直接返回
        if response_model is None:
            return result

        # 修复各种格式问题
        fixed_result = self._fix_response_format(result, response_model)
        
        if fixed_result != result:
            logger.debug(f"BailianOpenAIClient fixed response format for {response_model.__name__}")
        
        return fixed_result

    def _fix_response_format(self, result: Any, response_model: type) -> dict[str, Any]:
        """修复 LLM 返回的各种格式问题"""
        
        # 情况 1: 返回的是 list 而不是 dict
        # 例如 ExtractedEdges 期望 {"edges": [...]} 但得到了 [...]
        if isinstance(result, list):
            return self._wrap_list_in_dict(result, response_model)
        
        # 情况 2: 返回的是 JSON Schema 定义
        # 例如 {"properties": {"duplicate_facts": {...}}, "type": "object"}
        if isinstance(result, dict) and "properties" in result and "type" in result:
            return self._unwrap_schema_or_extract_data(result, response_model)
        
        # 情况 3: 返回的是 dict 但缺少必需字段
        if isinstance(result, dict):
            return self._fix_missing_fields(result, response_model)
        
        return result

    def _wrap_list_in_dict(self, result: list, response_model: type) -> dict[str, Any]:
        """将 list 包装成 dict，根据 response_model 的字段名"""
        # 找到 response_model 中类型为 list 的字段
        list_fields = []
        for field_name, field_info in response_model.model_fields.items():
            field_type = str(field_info.annotation).lower()
            if "list" in field_type:
                list_fields.append(field_name)
        
        # 如果只有一个 list 字段，用这个字段名包装
        if len(list_fields) == 1:
            return {list_fields[0]: result}
        
        # 如果有多个 list 字段，返回空数据（让重试机制处理）
        return self._get_default_values(response_model)

    def _unwrap_schema_or_extract_data(self, result: dict, response_model: type) -> dict[str, Any]:
        """处理 JSON Schema 定义：解包 properties 或返回默认值"""
        properties = result.get("properties", {})
        
        if not isinstance(properties, dict):
            return self._get_default_values(response_model)
        
        # 检查 properties 里的值是数据还是 schema 定义
        first_value = next(iter(properties.values()), None)
        
        if first_value is None:
            return self._get_default_values(response_model)
        
        # 如果值是基本类型（list, str, int 等），说明是数据实例
        if not isinstance(first_value, dict):
            return properties
        
        # 如果值是 dict 且包含 "type" key，说明是 schema 定义
        if isinstance(first_value, dict) and "type" in first_value:
            return self._get_default_values(response_model)
        
        # 其他情况，尝试返回 properties
        return properties

    def _fix_missing_fields(self, result: dict, response_model: type) -> dict[str, Any]:
        """修复缺少的必需字段"""
        # 检查是否缺少必需字段
        required_fields = set()
        for field_name, field_info in response_model.model_fields.items():
            if field_info.is_required():
                required_fields.add(field_name)
        
        missing_fields = required_fields - set(result.keys())
        
        if not missing_fields:
            return result
        
        # 补充缺少的字段
        fixed_result = result.copy()
        for field_name in missing_fields:
            field_info = response_model.model_fields[field_name]
            fixed_result[field_name] = self._get_field_default(field_info)
        
        return fixed_result

    def _get_field_default(self, field_info: Any) -> Any:
        """根据字段类型返回默认值"""
        # 如果有默认值且不是 PydanticUndefined，使用默认值
        if field_info.default is not None and field_info.default is not PydanticUndefined:
            return field_info.default
        
        # 根据字段类型返回默认值
        field_type = str(field_info.annotation).lower()
        
        if "list" in field_type:
            return []
        elif "dict" in field_type:
            return {}
        elif "int" in field_type:
            return 0
        elif "float" in field_type:
            return 0.0
        elif "bool" in field_type:
            return False
        elif "str" in field_type:
            return ""
        else:
            return None

    def _get_default_values(self, response_model: type) -> dict[str, Any]:
        """根据 response_model 返回所有字段的默认值"""
        defaults = {}
        for field_name, field_info in response_model.model_fields.items():
            defaults[field_name] = self._get_field_default(field_info)
        return defaults
