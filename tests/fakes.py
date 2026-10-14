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


def expansion_report(level, source=SOURCE):
    count = {"light": 0, "medium": 2, "rich": 5}[level]
    return {
        "original_pius": [{"fact": "one cat", "evidence": source}],
        "semantic_preservation": True,
        "no_contradiction": True,
        "meaningful_addition": True,
        "valid_format": True,
        "preserved_pius": [{"original_index": 0, "output_evidence": "A cat"}],
        "missing_pius": [],
        "contradictions": [],
        "added_pius": [
            {
                "fact": detail,
                "output_evidence": detail,
                "relevant": True,
                "novel": True,
                "visual": True,
            }
            for detail in ADDITIONS[:count]
        ],
        "insufficient_expansion_space": False,
        "feedback": "",
    }


def set_report(source=SOURCE):
    reports = {level: expansion_report(level, source) for level in PROMPTS}
    originals = reports["light"]["original_pius"]
    for report in reports.values():
        report.pop("original_pius")
    return {
        "original_pius": originals,
        "levels": reports,
        "retry_levels": [],
        "insufficient_expansion_space": False,
        "feedback": "",
    }


def response_for(stage, payload):
    if stage == "generate":
        language = payload["sample"].get("language")
        prompt = {"en": "A cat.", "mixed": "一只 cat"}.get(language, SOURCE)
        return {"concept": "cat", "prompt": prompt}
    if stage == "validate_source":
        return source_report(payload["sample"], payload["prompt"])
    if stage == "expand":
        return {"prompt": PROMPTS[payload["level"]]}
    if stage == "validate_english":
        return expansion_report(payload["level"], payload["source"])
    if stage == "validate_group":
        return set_report(payload["source"])
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
        scripted = self.responses[(stage, payload.get("level"))]
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
        ExpansionDraft,
        ExpansionSetValidation,
        ExpansionValidation,
        SourceDraft,
        SourceValidation,
    )

    schema = json.loads(inputs[0]["content"].rsplit("formatting:\n", 1)[1])
    stages = {
        SourceDraft.__name__: "generate",
        SourceValidation.__name__: "validate_source",
        ExpansionDraft.__name__: "expand",
        ExpansionValidation.__name__: "validate_english",
        ExpansionSetValidation.__name__: "validate_group",
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
