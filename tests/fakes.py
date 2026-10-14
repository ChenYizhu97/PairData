"""小型模型替身，只构造这些测试所需的响应。

Small model fakes construct only the responses needed by these tests.
"""

from collections import defaultdict
from pathlib import Path

from midasdata.config import load_settings
from midasdata.model import LLMModel

SOURCE = "一只猫"
PROMPTS = {
    "light": "A cat.",
    "medium": "A cat on a chair in daylight.",
    "rich": "A cat on a chair in daylight beside a window, wearing a red collar, with a blue bowl.",
}
ADDITIONS = ["on a chair", "in daylight", "beside a window", "a red collar", "a blue bowl"]


def source_report(conditions=None, prompt=SOURCE):
    return {
        "conditions": {
            dimension: {"satisfied": True, "evidence": prompt} for dimension in (conditions or {})
        },
        "visual": True,
        "clean": True,
        "visual_evidence": prompt,
        "feedback": "",
    }


def expansion_report(level):
    count = {"light": 0, "medium": 2, "rich": 5}[level]
    return {
        "passed": True,
        "reason": "Source preserved; additions are meaningful.",
        "added_pius": [{"fact": detail, "output_evidence": detail} for detail in ADDITIONS[:count]],
    }


def expansion_draft(level):
    return {"prompt": PROMPTS[level]}


def request_level(stage, payload):
    # 测试根据目标数量或文本定位脚本响应，模型请求本身不含 level。
    # Tests locate scripted responses by target count or text; model requests contain no level.
    if stage == "expand":
        count = payload["target_added_pius"]
        return "light" if count == 0 else "medium" if count <= 4 else "rich"
    if stage == "blind_expand_validation":
        return next(level for level, prompt in PROMPTS.items() if prompt == payload["output"])
    return None


def response_for(stage, payload):
    if stage == "generate":
        language = payload["sample"].get("language")
        prompt = {"en": "A cat.", "mixed": "一只 cat"}.get(language, SOURCE)
        return {"concept": "cat", "prompt": prompt}
    if stage == "blind_sampler_validation":
        return source_report(payload["sample"], payload["prompt"])
    if stage == "expand":
        return expansion_draft(request_level(stage, payload))
    if stage == "blind_expand_validation":
        return expansion_report(request_level(stage, payload))
    raise AssertionError(stage)


class FakeModel(LLMModel):
    def __init__(self):
        self.name = "fake-model"
        self._api_key = ""
        self.responses = defaultdict(list)
        self.calls = []

    async def invoke(self, inputs, schema):
        stage, payload = model_input(inputs)
        self.calls.append((stage, payload))
        scripted = self.responses[(stage, request_level(stage, payload))]
        response = scripted.pop(0) if scripted else response_for(stage, payload)
        if isinstance(response, Exception):
            raise response
        return {"parsed": response}


def local_settings(directory: Path):
    env_file = directory / "test.env"
    env_file.write_text(
        "MODEL_NAME=test-model\nOPENAI_BASE_URL=http://127.0.0.1:1/v1\nOPENAI_API_KEY=test-secret\n"
    )
    return load_settings(None, {"retry_initial": 0.0, "retry_max": 0.0}, env_file)


def model_input(inputs):
    import json

    from midasdata.schemas import (
        BlindExpansionValidation,
        ExpansionDraft,
        SourceDraft,
        SourceValidation,
    )

    schema = json.loads(inputs[0]["content"].rsplit("formatting:\n", 1)[1])
    stages = {
        SourceDraft.__name__: "generate",
        SourceValidation.__name__: "blind_sampler_validation",
        ExpansionDraft.__name__: "expand",
        BlindExpansionValidation.__name__: "blind_expand_validation",
    }
    return stages[schema["title"]], json.loads(inputs[1]["content"])


def transport_response(inputs):
    """让 CLI 集成测试穿过真实的提示词构造、解析和审计边界。

    Exercise real prompt construction, parsing and auditing in CLI integration tests.
    """
    import json

    from langchain_core.messages import AIMessage

    stage, payload = model_input(inputs)
    return AIMessage(
        content=json.dumps(response_for(stage, payload)),
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )
