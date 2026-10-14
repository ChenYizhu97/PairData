"""模型接口：发送消息、保留原始响应并解析结构化输出。

Model interface: send messages, retain raw responses and parse structured output.
"""

import asyncio
from typing import TypeVar

import openai
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from midasdata.config import ConfigurationError, ModelSettings

Response = TypeVar("Response", bound=BaseModel)


class LLMModel:
    """一个配置好的模型；运行历史和业务质量判断由上层处理。

    One configured model; execution history and business quality belong to higher layers.
    """

    def __init__(self, settings: ModelSettings, *, api_key: str, timeout: float):
        self.name = settings.model
        self.structured_output = settings.structured_output
        self.timeout = timeout
        self._api_key = api_key
        self.client = ChatOpenAI(
            model=settings.model,
            base_url=settings.base_url,
            api_key=api_key,
            temperature=settings.temperature,
            reasoning_effort=settings.reasoning_effort,
            extra_body=settings.extra_body or None,
            timeout=timeout,
            max_retries=0,
        )

    def error_text(self, error: Exception) -> str:
        description = f"{type(error).__name__}: {error}"
        if self._api_key:
            description = description.replace(self._api_key, "[REDACTED]")
        return description[:2000]

    @staticmethod
    def is_retryable(error: Exception) -> bool:
        if isinstance(error, openai.APIStatusError):
            return error.status_code in {408, 409, 429} or error.status_code >= 500
        return isinstance(error, (TimeoutError, openai.APIConnectionError, ValueError))

    async def invoke(self, inputs: list[dict], schema: type[BaseModel]) -> dict:
        """执行一次请求，返回可落盘的响应；由 parse 单独检查输出契约。

        Make one request and return a persistable response; parse checks its output contract.
        """
        model = self.client
        native = self.structured_output == "native"
        if native:
            model = model.with_structured_output(
                schema, method="json_schema", include_raw=True, strict=False
            )
        try:
            response = await asyncio.wait_for(model.ainvoke(inputs), timeout=self.timeout)
        except openai.APIStatusError as error:
            if not self.is_retryable(error):
                raise ConfigurationError(self.error_text(error)) from None
            raise
        raw = response["raw"] if native else response
        parsed = response.get("parsed") if native else None
        return {
            "raw": raw.content,
            "parsed": parsed.model_dump() if isinstance(parsed, BaseModel) else parsed,
            "usage": raw.usage_metadata or {},
        }

    @staticmethod
    def parse(data: dict, schema: type[Response]) -> Response:
        """解析新响应或历史响应；不计算业务质量结果。

        Parse a new or saved response without assessing business quality.
        """
        if data.get("parsed") is not None:
            return schema.model_validate(data["parsed"])
        return schema.model_validate_json(data["raw"])
