"""Send configured messages and retain responses; local Schema validation is separate."""

import asyncio

import openai
from langchain_openai import ChatOpenAI

from pairdata.config import ConfigurationError, ModelSettings


class LLMModel:
    """One configured model, independent of task state and quality decisions."""

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

    async def invoke(self, inputs: list[dict], schema: dict) -> dict:
        """Make one request and return a persistable response for Schema validation."""
        model = self.client
        native = self.structured_output == "native"
        if native:
            model = model.with_structured_output(
                {"title": "RecipeResponse", "description": "Recipe-defined response", **schema},
                method="json_schema",
                include_raw=True,
                strict=False,
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
            "parsed": parsed,
            "usage": raw.usage_metadata or {},
        }
