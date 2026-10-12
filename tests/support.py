"""Small deterministic responses for the current contracts, created afresh per test."""

from collections import defaultdict

from midasdata.config import Config
from midasdata.schemas import LEVELS, SourceValidation

SOURCE = "一只猫"
ADDITIONS = ("orange fur", "on a rug", "by a window", "morning light", "green eyes", "curled tail")
OUTPUTS = {
    "light": "A cat.",
    "medium": "A cat with orange fur, on a rug, by a window.",
    "rich": "A cat with orange fur, on a rug, by a window, in morning light, with green eyes and a curled tail.",
}


def source_validation(sample=None, prompt=SOURCE):
    return SourceValidation(
        conditions={key: {"satisfied": True, "evidence": prompt} for key in (sample or {})},
        visual=True,
        clean=True,
        visual_evidence=prompt,
        feedback="",
    ).model_dump()


def english_validation(level, source=SOURCE):
    count = {"light": 0, "medium": 3, "rich": 6}[level]
    return {
        "original_pius": [{"fact": "A cat", "evidence": source}],
        "semantic_preservation": True,
        "no_contradiction": True,
        "meaningful_addition": level != "light",
        "valid_format": True,
        "preserved_pius": [{"original_index": 0, "output_evidence": "A cat"}],
        "missing_pius": [],
        "contradictions": [],
        "added_pius": [
            {"fact": fact, "output_evidence": fact, "relevant": True, "novel": True, "visual": True}
            for fact in ADDITIONS[:count]
        ],
        "insufficient_expansion_space": False,
        "feedback": "",
    }


def expansion_case(source=SOURCE):
    levels = {level: english_validation(level, source) for level in LEVELS}
    originals = levels["light"]["original_pius"]
    for validation in levels.values():
        del validation["original_pius"]
    return {
        "source": source,
        "outputs": dict(OUTPUTS),
        "validation": {
            "original_pius": originals,
            "levels": levels,
            "retry_levels": [],
            "insufficient_expansion_space": False,
            "feedback": "",
        },
    }


def result_examples():
    return [
        {
            "id": "generated",
            "workflow": "generate",
            "status": "passed",
            "source": {"id": "generated", "sample": {"language": "zh"}},
            "records": [{"id": "generated", "sample": {"language": "zh"}, "prompt": SOURCE}],
            "details": {"prompt": SOURCE, "validation": source_validation({"language": "zh"})},
        },
        {
            "id": "expanded",
            "workflow": "expand",
            "status": "passed",
            "source": {"id": "expanded", "prompt": SOURCE},
            "records": [
                {"id": "expanded", "level": level, "prompt": OUTPUTS[level]} for level in LEVELS
            ],
            "details": {
                "round": 1,
                "levels": {
                    level: {
                        "level": level,
                        "round": 1,
                        "generation_attempt": 1,
                        "status": "passed",
                        "prompt": OUTPUTS[level],
                        "validation": english_validation(level),
                    }
                    for level in LEVELS
                },
            },
        },
    ]


class ScriptedCaller:
    """Exercise graph routing with controlled current responses and failures."""

    def __init__(self):
        self.config = Config()
        self.responses = defaultdict(list)
        self.calls = []
        self.decisions = []

    async def call(self, stage, payload, schema, *, check=None, **context):
        self.calls.append({"stage": stage, "payload": payload, **context})
        responses = self.responses[(stage, context.get("level"))]
        if responses:
            response = responses.pop(0)
        elif stage == "generate":
            response = {"concept": "猫", "prompt": SOURCE}
        elif stage == "validate_source":
            response = source_validation(payload["sample"], payload["prompt"])
        elif stage == "expand":
            response = {"prompt": OUTPUTS[context["level"]]}
        elif stage == "validate_english":
            response = english_validation(context["level"], payload["source"])
        elif stage == "validate_group":
            response = expansion_case()["validation"]
        else:
            raise AssertionError(f"unexpected stage: {stage}")
        if isinstance(response, Exception):
            raise response
        value = schema.model_validate(response)
        if check:
            check(value)
        return value

    def record_decision(self, stage, decision, **context):
        self.decisions.append({"stage": stage, "decision": decision, **context})
