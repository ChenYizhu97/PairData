"""Generator planning rules and validator evidence rules respect their schemas."""

import json
import unittest

from midasdata.prompts import PIU_VALIDATION_RULES, messages, sft_record
from midasdata.schemas import (
    EnglishDraft,
    EnglishValidation,
    GroupValidation,
    SourceDraft,
    SourceValidation,
)


class PromptContractTest(unittest.TestCase):
    def test_expander_defines_piu_without_requesting_validator_fields(self):
        prompt = messages("expand", {"source": "一只猫", "level": "medium"}, EnglishDraft)[0][
            "content"
        ]
        self.assertIn("valid visual information element", prompt)
        self.assertIn("Added PIUs must be relevant, non-redundant, and visual", prompt)
        self.assertIn("Only return a JSON object containing a prompt field", prompt)
        self.assertNotIn(PIU_VALIDATION_RULES, prompt)
        schema = json.loads(prompt.rsplit("formatting:\n", 1)[1])
        self.assertEqual(set(schema["properties"]), {"prompt"})
        self.assertEqual(schema["required"], ["prompt"])
        self.assertFalse(schema["additionalProperties"])

    def test_validators_require_piu_evidence_and_alignment_fields(self):
        for stage, schema in (
            ("validate_english", EnglishValidation),
            ("validate_group", GroupValidation),
        ):
            with self.subTest(stage=stage):
                prompt = messages(stage, {}, schema)[0]["content"]
                instructions = prompt.split("\nOnly return a JSON object conforming", 1)[0]
                self.assertIn("valid visual information element", instructions)
                for field in (
                    "evidence",
                    "original_index",
                    "output_evidence",
                    "relevant",
                    "novel",
                    "visual",
                ):
                    self.assertIn(field, instructions)
                self.assertIn("continuous, verbatim substring", instructions)

    def test_all_prompts_embed_the_same_schema_used_for_native_output(self):
        for stage, model in (
            ("generate", SourceDraft),
            ("validate_source", SourceValidation),
            ("expand", EnglishDraft),
            ("validate_english", EnglishValidation),
            ("validate_group", GroupValidation),
        ):
            with self.subTest(stage=stage):
                prompt = messages(stage, {}, model)[0]["content"]
                schema = json.loads(prompt.rsplit("formatting:\n", 1)[1])
                self.assertEqual(schema, model.model_json_schema())

    def test_group_schema_extracts_original_pius_only_once(self):
        schema = GroupValidation.model_json_schema()
        level_schema = schema["$defs"]["LevelValidation"]
        self.assertIn("original_pius", schema["required"])
        self.assertNotIn("original_pius", level_schema["properties"])
        self.assertFalse(level_schema["additionalProperties"])
        self.assertIn("original_pius", EnglishValidation.model_json_schema()["required"])

    def test_sft_uses_authoritative_english_instructions_and_preserves_source(self):
        source = "一只 red fox"
        record = sft_record(source, "light", "A red fox.")
        self.assertEqual(
            [item["role"] for item in record["messages"]], ["system", "user", "assistant"]
        )
        self.assertTrue(record["messages"][0]["content"].startswith("Translate and expand"))
        self.assertEqual(
            record["messages"][1]["content"],
            "Expansion level: light\nSource description: " + source,
        )
        self.assertEqual(record["messages"][2]["content"], "A red fox.")
