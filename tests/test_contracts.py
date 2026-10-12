import copy
import json
import os
import random
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from pydantic import ValidationError

from midasdata.config import (
    Config,
    ConfigurationError,
    ModelConfig,
    check_credentials,
    load_config,
    load_env,
)
from midasdata.io import RunStore, input_records
from midasdata.preview import safe_json
from midasdata.sampler import load_sampler, sample_conditions
from midasdata.schemas import (
    ConditionCheck,
    EnglishValidation,
    ExpandDetails,
    ExpandedRecord,
    GenerateDetails,
    GeneratedRecord,
    GroupValidation,
    Sample,
    SampleResult,
    SourceValidation,
)
from midasdata.validation import english_decision, group_decision, source_decision
from tests.support import OUTPUTS, SOURCE, english_validation, expansion_case, result_examples


@contextmanager
def isolated_environment():
    previous = os.environ.copy()
    os.environ.clear()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


class ContractsTest(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.light = {
            "source": SOURCE,
            "output": OUTPUTS["light"],
            "validation": english_validation("light"),
        }
        self.group = expansion_case()

    def test_sampler_reproducible_and_spec_weights(self):
        sampler = load_sampler()
        a, b = random.Random(42), random.Random(42)
        self.assertEqual(
            [sample_conditions(a, sampler) for _ in range(100)],
            [sample_conditions(b, sampler) for _ in range(100)],
        )
        self.assertEqual(
            tuple(sampler), ("domain", "focus", "specificity", "expression", "language")
        )
        self.assertEqual(sample_conditions(a, sampler, 1), {})
        rows = [sample_conditions(a, sampler) for _ in range(10000)]
        for dimension in sampler:
            dropped = sum(dimension not in row for row in rows) / len(rows)
            self.assertTrue(0.08 < dropped < 0.12)

    def test_sampler_requires_five_dimensions_and_separates_language_from_expression(self):
        sampler = load_sampler()
        self.assertEqual(set(sampler), {"domain", "focus", "specificity", "expression", "language"})
        self.assertEqual({row[0] for row in sampler["language"]}, {"zh", "en", "mixed"})
        self.assertNotIn("mixed", {row[0] for row in sampler["expression"]})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sampler.json"
            for changed in (
                {key: rows for key, rows in sampler.items() if key != "language"},
                {**sampler, "language": [["fr", "French", 1.0]]},
                {**sampler, "expression": [["mixed", "Mixed", 1.0]]},
                list(sampler),
            ):
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    load_sampler(path)

    def test_language_business_contract_and_input_round_trip(self):
        for language, prompt in (("zh", "一只猫"), ("en", "A cat"), ("mixed", "一只 red cat")):
            with self.subTest(language=language):
                row = {
                    "id": language,
                    "prompt": prompt,
                    "sample": {"language": language, "expression": "natural"},
                }
                self.assertEqual(GeneratedRecord.model_validate(row).model_dump(), row)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "input.jsonl"
                    path.write_text(json.dumps(row) + "\n")
                    self.assertEqual(list(input_records(path)), [row])
        with self.assertRaises(ValidationError):
            Sample(expression="mixed")

    def test_language_checks_follow_sampled_conditions_and_allow_dropout(self):
        for language, prompt, expected in (
            ("zh", "一只猫", True),
            ("zh", "A cat", False),
            ("en", "A red cat", True),
            ("en", "一只 red cat", False),
            ("en", "123", False),
            ("mixed", "一只 red cat", True),
            ("mixed", "一只猫", False),
            ("mixed", "A cat", False),
            (None, "A cat", True),
            (None, "一只猫", True),
            (None, "```A cat```", False),
        ):
            with self.subTest(language=language, prompt=prompt):
                sample = {"language": language} if language else {}
                validation = SourceValidation(
                    conditions={
                        key: ConditionCheck(satisfied=True, evidence=prompt) for key in sample
                    },
                    visual=True,
                    clean=True,
                    visual_evidence=prompt,
                    feedback="",
                )
                self.assertEqual(source_decision(validation, sample, prompt).passed, expected)

    def test_sampler_invalid_weights_and_duplicates(self):
        sampler = load_sampler()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sampler.json"
            for mutate in (
                lambda s: s["domain"][0].__setitem__(2, -0.2),
                lambda s: s["domain"].append(s["domain"][0]),
                lambda s: s["domain"][0].__setitem__(2, 0.1),
            ):
                changed = copy.deepcopy(sampler)
                mutate(changed)
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    load_sampler(path)

    def test_sampler_rejects_null_options_without_affecting_dropout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sampler.json"
            for dimension in load_sampler():
                with self.subTest(dimension=dimension):
                    sampler = load_sampler()
                    sampler[dimension] = [[None, "维度缺省应使用 dropout", 1.0]]
                    path.write_text(json.dumps(sampler))
                    with self.assertRaisesRegex(ValueError, f"{dimension}: option"):
                        load_sampler(path)
            self.assertEqual(sample_conditions(random.Random(0), load_sampler(), 1.0), {})

    def test_contract_invalid_level_or_empty_text(self):
        for row in (
            {"id": "a", "level": "extreme", "prompt": "x"},
            {"id": "a", "level": "light", "prompt": "  "},
        ):
            with self.assertRaises(ValidationError):
                ExpandedRecord.model_validate(row)

    def test_env_loading_and_cli_precedence_no_shell_evaluation(self):
        with tempfile.TemporaryDirectory() as directory, isolated_environment():
            env = Path(directory) / ".env"
            env.write_text(
                'base_url=https://example.com/v1\nmodel=example-model\napi_key="secret$(touch /tmp/no-execute)"\n'
            )
            values = load_env(env)
            self.assertEqual(values["OPENAI_API_KEY"], "secret$(touch /tmp/no-execute)")
            path = Path(directory) / "config.toml"
            path.write_text('concurrency=2\n[generator]\nmodel="${MODEL_NAME}"\n')
            config = load_config(path, {"model": "override", "concurrency": 3}, env)
            self.assertEqual(config.generator.model, "override")
            self.assertEqual(config.validator.model, "example-model")
            self.assertEqual(config.concurrency, 3)
            self.assertNotIn("secret", config.model_dump_json())
            path.write_text('[generator]\nmodel="${MISSING_MODEL}"\n')
            with self.assertRaises(ConfigurationError):
                load_config(path, {}, Path(directory) / "missing")
            self.assertEqual(
                load_config(
                    path, {"model": "override"}, Path(directory) / "missing"
                ).generator.model,
                "override",
            )

    def test_invalid_configuration(self):
        for data in (
            {"concurrency": 0},
            {"timeout": 0.0},
            {"piu": {"medium": {"min": 4, "max": 2}}},
            {"retry_initial": 2.0, "retry_max": 1.0},
            {"unknown_field": 1},
        ):
            with self.assertRaises(ValidationError):
                Config.model_validate(data)
        with self.assertRaises(ValidationError):
            ModelConfig(base_url="ftp://example.com")

    def test_model_configuration_requires_a_toml_table(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for role in ("generator", "validator"):
                for value in ('"model-name"', '[]', '42'):
                    with self.subTest(role=role, value=value):
                        path.write_text(f"{role} = {value}\n")
                        with self.assertRaisesRegex(ConfigurationError, f"{role}.*TOML table"):
                            load_config(path, {}, Path(directory) / "missing.env")

    def test_config_loads_are_independent_and_do_not_modify_the_process(self):
        with tempfile.TemporaryDirectory() as directory, isolated_environment():
            first_env, second_env = (Path(directory) / name for name in ("first.env", "second.env"))
            first_env.write_text(
                "model=first\napi_key=first-secret\nbase_url=https://first.example/v1\n"
            )
            second_env.write_text(
                "model=second\napi_key=second-secret\nbase_url=https://second.example/v1\n"
            )
            first = load_config(None, {}, first_env)
            second = load_config(None, {}, second_env)
            self.assertEqual(first.generator.model, "first")
            self.assertEqual(second.generator.model, "second")
            self.assertEqual(first.api_key(first.generator), "first-secret")
            self.assertEqual(second.api_key(second.generator), "second-secret")
            self.assertEqual(dict(os.environ), {})
            self.assertNotIn("first-secret", first.model_dump_json())
            self.assertNotIn("second-secret", second.model_dump_json())
            empty = load_config(None, {}, Path(directory) / "missing")
            self.assertEqual(empty.generator.model, "unset")
            self.assertIsNone(empty.api_key(empty.generator))
            with self.assertRaises(ConfigurationError):
                check_credentials(empty)
            os.environ["OPENAI_API_KEY"] = "changed-after-loading"
            self.assertEqual(first.api_key(first.generator), "first-secret")
            self.assertIsNone(empty.api_key(empty.generator))

    def test_deepseek_defaults_disable_thinking_for_both_roles(self):
        with tempfile.TemporaryDirectory() as directory, isolated_environment():
            env = Path(directory) / ".env"
            env.write_text("model=deepseek-flash\nbase_url=https://api.deepseek.com\n")
            config = load_config(None, {}, env)
            for role in ("generator", "validator"):
                settings = getattr(config, role)
                self.assertEqual(settings.extra_body, {"thinking": {"type": "disabled"}})
                self.assertIsNone(settings.reasoning_effort)
            config.generator.extra_body["thinking"]["type"] = "enabled"
            self.assertEqual(config.validator.extra_body["thinking"]["type"], "disabled")

    def test_explicit_thinking_and_other_provider_settings_are_preserved(self):
        body = {"thinking": {"type": "enabled"}, "custom": [1, 2]}
        settings = ModelConfig(base_url="https://api.deepseek.com", extra_body=body)
        self.assertEqual(settings.extra_body, body)
        self.assertEqual(ModelConfig().extra_body, {})
        self.assertEqual(ModelConfig(base_url="https://deepseek.com.example/v1").extra_body, {})
        self.assertEqual(
            ModelConfig(base_url="https://api.deepseek.com", extra_body={"custom": 1}).extra_body,
            {"thinking": {"type": "disabled"}, "custom": 1},
        )

    def test_process_environment_overrides_env_file_across_aliases(self):
        families = (
            ("MODEL_NAME", "model", "MODEL"),
            ("OPENAI_BASE_URL", "base_url", "BASE_URL"),
            ("OPENAI_API_KEY", "api_key", "API_KEY"),
        )
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            for canonical, *aliases in families:
                for process_key in (canonical, *aliases):
                    for file_key in (canonical, *aliases):
                        with self.subTest(process_key=process_key, file_key=file_key):
                            with isolated_environment():
                                os.environ[process_key] = "from-process"
                                env.write_text(f"{file_key}=from-file\n")
                                values = load_env(env)
                                self.assertEqual(values[canonical], "from-process")
                                self.assertEqual(os.environ[process_key], "from-process")

    def test_canonical_environment_name_wins_within_each_source(self):
        with tempfile.TemporaryDirectory() as directory, isolated_environment():
            env = Path(directory) / ".env"
            env.write_text("model=alias-file\nMODEL_NAME=canonical-file\n")
            self.assertEqual(load_config(None, {}, env).generator.model, "canonical-file")
            os.environ["model"] = "alias-process"
            os.environ["MODEL_NAME"] = "canonical-process"
            self.assertEqual(load_config(None, {}, env).generator.model, "canonical-process")

    def test_config_interpolates_nested_lists_and_dicts(self):
        with tempfile.TemporaryDirectory() as directory, isolated_environment():
            os.environ["BODY_VALUE"] = "resolved-value"
            path = Path(directory) / "config.toml"
            path.write_text(
                "[generator.extra_body]\n"
                'scalar="${BODY_VALUE}"\n'
                'items=["${BODY_VALUE}", {nested=[{deep="${BODY_VALUE}"}]}, 1, false]\n'
            )
            config = load_config(path, {}, Path(directory) / "missing")
            self.assertEqual(
                config.generator.extra_body,
                {
                    "scalar": "resolved-value",
                    "items": ["resolved-value", {"nested": [{"deep": "resolved-value"}]}, 1, False],
                },
            )
            del os.environ["BODY_VALUE"]
            path.write_text('[generator.extra_body]\nitems=[{nested=["${BODY_VALUE}"]}]\n')
            with self.assertRaisesRegex(ConfigurationError, "BODY_VALUE is missing"):
                load_config(path, {}, Path(directory) / "missing")

    def test_model_and_credentials_are_always_required(self):
        with isolated_environment():
            with self.assertRaisesRegex(ConfigurationError, "model.*required"):
                check_credentials(self.config)
            config = Config(
                generator=ModelConfig(model="example"), validator=ModelConfig(model="example")
            )
            with self.assertRaisesRegex(ConfigurationError, "missing API key"):
                check_credentials(config)
            os.environ["OPENAI_API_KEY"] = "test-only"
            check_credentials(config)

    def test_duplicate_input_and_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.jsonl"
            row = {"id": "a", "prompt": self.light["source"], "sample": {"domain": "vehicles"}}
            path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
            with self.assertRaisesRegex(ValueError, "duplicate input ID"):
                list(input_records(path))

    def test_resume_rejects_prompt_text_changes_without_a_version_change(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run"
            store = RunStore(path)
            store.prepare("generate", self.config, {"count": 0, "seed": 0}, load_sampler(), False)
            store.close()
            changed = RunStore(path)
            try:
                with patch("midasdata.io.file_digest", return_value="changed-prompt-content"):
                    with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
                        changed.prepare(
                            "generate", self.config, {"count": 0, "seed": 0}, load_sampler(), True
                        )
            finally:
                changed.close()

    def test_dropout_requires_only_actual_conditions(self):
        result = SourceValidation(
            conditions={}, visual=True, clean=True, visual_evidence="猫", feedback=""
        )
        self.assertTrue(source_decision(result, {}, self.light["source"]).passed)
        self.assertFalse(
            source_decision(result, {"domain": "vehicles"}, self.light["source"]).passed
        )

    def test_evidence_and_boolean_cannot_override_omission(self):
        source, output = self.light["source"], self.light["output"]
        raw = copy.deepcopy(self.light["validation"])
        raw["preserved_pius"].pop()
        decision = english_decision(
            EnglishValidation.model_validate(raw), source, output, "light", self.config.piu
        )
        self.assertFalse(decision.passed)
        raw = copy.deepcopy(self.light["validation"])
        raw["preserved_pius"][0]["original_index"] = 50
        self.assertFalse(
            english_decision(
                EnglishValidation.model_validate(raw), source, output, "light", self.config.piu
            ).passed
        )
        for key, value in (
            ("missing_pius", ["数量"]),
            ("contradictions", ["违反否定"]),
            ("valid_format", False),
        ):
            raw = copy.deepcopy(self.light["validation"])
            raw[key] = value
            self.assertFalse(
                english_decision(
                    EnglishValidation.model_validate(raw), source, output, "light", self.config.piu
                ).passed
            )

    def test_light_without_additions_is_not_rejected_for_false_addition_flag(self):
        fixture = self.light
        validation = EnglishValidation.model_validate(fixture["validation"])
        self.assertFalse(validation.meaningful_addition)
        decision = english_decision(
            validation, fixture["source"], fixture["output"], "light", self.config.piu
        )
        self.assertTrue(decision.passed)
        validation.missing_pius = ["跳跃"]
        self.assertFalse(
            english_decision(
                validation, fixture["source"], fixture["output"], "light", self.config.piu
            ).passed
        )

    def test_piu_boundaries_meaningless_and_duplicate_additions(self):
        source = self.group["source"]
        for level in ("light", "medium", "rich"):
            validation = EnglishValidation(
                **self.group["validation"]["levels"][level],
                original_pius=self.group["validation"]["original_pius"],
            )
            self.assertTrue(
                english_decision(
                    validation, source, self.group["outputs"][level], level, self.config.piu
                ).passed
            )
        for level, limits in (
            ("medium", (4, 4)),
            ("medium", (2, 2)),
            ("rich", (7, 8)),
            ("rich", (5, 5)),
        ):
            piu = Config(piu={level: {"min": limits[0], "max": limits[1]}}).piu
            validation = EnglishValidation(
                **self.group["validation"]["levels"][level],
                original_pius=self.group["validation"]["original_pius"],
            )
            self.assertFalse(
                english_decision(
                    validation, source, self.group["outputs"][level], level, piu
                ).passed
            )
        output = self.group["outputs"]["rich"]
        raw = {
            **copy.deepcopy(self.group["validation"]["levels"]["rich"]),
            "original_pius": self.group["validation"]["original_pius"],
        }
        for duplicate in ("ORANGE FUR", "orange-fur", "orange_fur"):
            with self.subTest(duplicate=duplicate):
                raw["added_pius"][0]["fact"] = "orange fur"
                raw["added_pius"][1]["fact"] = duplicate
                decision = english_decision(
                    EnglishValidation.model_validate(raw), source, output, "rich", self.config.piu
                )
                self.assertIn("invalid_added_evidence", decision.reasons)
        raw = {
            **copy.deepcopy(self.group["validation"]["levels"]["rich"]),
            "original_pius": self.group["validation"]["original_pius"],
        }
        raw["added_pius"][1]["visual"] = False
        self.assertFalse(
            english_decision(
                EnglishValidation.model_validate(raw), source, output, "rich", self.config.piu
            ).passed
        )

    def test_group_unified_pius_and_invalid_retry_structure(self):
        source, outputs = self.group["source"], self.group["outputs"]
        raw = copy.deepcopy(self.group["validation"])
        self.assertTrue(
            group_decision(
                GroupValidation.model_validate(raw), source, outputs, self.config.piu
            ).passed
        )
        raw["levels"]["medium"]["original_pius"] = []
        with self.assertRaises(ValidationError):
            group_decision(GroupValidation.model_validate(raw), source, outputs, self.config.piu)
        raw = copy.deepcopy(self.group["validation"])
        raw["levels"]["rich"]["contradictions"] = ["changed original time"]
        with self.assertRaisesRegex(ValueError, "affected retry_levels"):
            group_decision(GroupValidation.model_validate(raw), source, outputs, self.config.piu)

    def test_preview_safe_embedded_json(self):
        hostile = '</script><script>alert("x")</script>&\u2028'
        serialized = safe_json([{"prompt": hostile}])
        self.assertNotIn("</script>", serialized)
        self.assertEqual(json.loads(serialized)[0]["prompt"], hostile)

    def test_group_allows_different_added_time_when_source_has_no_time(self):
        source, outputs = self.group["source"], copy.deepcopy(self.group["outputs"])
        raw = copy.deepcopy(self.group["validation"])
        for level, word in (("medium", "sunset"), ("rich", "midday")):
            outputs[level] += " " + word
            raw["levels"][level]["added_pius"][0].update(fact=word, output_evidence=word)
        self.assertTrue(
            group_decision(
                GroupValidation.model_validate(raw), source, outputs, self.config.piu
            ).passed
        )

    def test_group_checks_each_preservation_index_against_the_shared_list(self):
        for level in ("light", "medium", "rich"):
            raw = copy.deepcopy(self.group["validation"])
            raw["levels"][level]["preserved_pius"][0]["original_index"] = len(raw["original_pius"])
            raw["retry_levels"] = [level]
            raw["feedback"] = "Restore coverage of the common original PIUs."
            decision = group_decision(
                GroupValidation.model_validate(raw),
                self.group["source"],
                self.group["outputs"],
                self.config.piu,
            )
            self.assertFalse(decision.passed)
            self.assertIn(f"{level}:incomplete_preservation_evidence", decision.reasons)

    def test_group_shortage_at_either_scope_is_terminal_without_revision_metadata(self):
        for scope in ("group", "light", "medium", "rich"):
            with self.subTest(scope=scope):
                raw = copy.deepcopy(self.group["validation"])
                target = raw if scope == "group" else raw["levels"][scope]
                target["insufficient_expansion_space"] = True
                raw["retry_levels"] = []
                raw["feedback"] = ""
                decision = group_decision(
                    GroupValidation.model_validate(raw),
                    self.group["source"],
                    self.group["outputs"],
                    self.config.piu,
                )
                self.assertFalse(decision.passed)
                self.assertTrue(decision.insufficient_expansion_space)
                self.assertEqual(decision.reasons, ["insufficient_expansion_space"])

    def test_terminal_group_still_requires_complete_structure_without_nested_originals(self):
        for mutate in (
            lambda value: value["levels"].pop("light"),
            lambda value: value["levels"]["rich"].__setitem__("original_pius", []),
            lambda value: value.__setitem__("retry_levels", ["rich", "rich"]),
        ):
            raw = copy.deepcopy(self.group["validation"])
            raw["insufficient_expansion_space"] = True
            mutate(raw)
            with self.assertRaises(ValueError):
                group_decision(
                    GroupValidation.model_validate(raw),
                    self.group["source"],
                    self.group["outputs"],
                    self.config.piu,
                )

    def test_result_types_and_source_metadata_follow_workflow(self):
        cases = result_examples()
        for case in cases:
            with self.subTest(workflow=case["workflow"], sample_id=case["id"]):
                result = SampleResult.model_validate(case)
                details_type = GenerateDetails if result.workflow == "generate" else ExpandDetails
                record_type = GeneratedRecord if result.workflow == "generate" else ExpandedRecord
                self.assertIsInstance(result.details, details_type)
                self.assertTrue(all(isinstance(row, record_type) for row in result.records))
                self.assertEqual(result.model_dump()["records"], case["records"])
                result.source["custom_metadata"] = {"value": [1, "text"]}
                self.assertEqual(
                    result.model_dump()["source"]["custom_metadata"], {"value": [1, "text"]}
                )
                empty_details = SampleResult.model_validate({**case, "details": {}})
                self.assertEqual(empty_details.details, details_type())
                self.assertEqual(empty_details.model_dump()["details"], details_type().model_dump())

    def test_result_rejects_wrong_workflow_records_and_details(self):
        cases = result_examples()
        generated = next(case for case in cases if case["workflow"] == "generate")
        expanded = next(case for case in cases if case["workflow"] == "expand")
        for field in ("records", "details"):
            for original, other in ((generated, expanded), (expanded, generated)):
                with self.subTest(workflow=original["workflow"], field=field):
                    raw = {**original, field: other[field]}
                    with self.assertRaises(ValidationError):
                        SampleResult.model_validate(raw)
        with self.assertRaises(ValidationError):
            SampleResult.model_validate({**expanded, "records": None})
        raw = copy.deepcopy(expanded)
        raw["details"]["levels"]["rich"]["generation_attempt"] = "1"
        with self.assertRaises(ValidationError):
            SampleResult.model_validate(raw)
