import unittest
from copy import deepcopy

import yaml

from pairdata.bindings import lookup
from pairdata.cli import create_parser
from pairdata.config import ConfigurationError
from pairdata.rules import Rule, evaluate_rule
from pairdata.sampler import Categorical, Integer, sample_fields
from tests.fakes import RecipeFixture


class ConfigurationTest(RecipeFixture, unittest.TestCase):
    def test_target_and_template_boundaries_are_checked_before_execution(self):
        original = yaml.safe_load(self.path.read_text())
        for key in ("source_sample", "evaluation", "generation"):
            config = deepcopy(original)
            config["target"]["generate"]["inputs"][key] = {"ref": key}
            self.path.write_text(yaml.safe_dump(config))
            with self.assertRaises(ValueError):
                self.load()
        self.path.write_text(yaml.safe_dump(original))
        (self.path.parent / "prompts/target_generate.j2").write_text("{{ unbound }}")
        with self.assertRaisesRegex(ConfigurationError, "unbound variables"):
            self.load()

    def test_rule_errors_cannot_pass_through_not_or_any(self):
        bad = {"field": "evaluation.missing", "op": "eq", "value": True}
        good = {"field": "evaluation.score", "op": "gte", "value": {"ref": "sample.threshold"}}
        context = {"evaluation": {"score": 0.9}, "sample": {"threshold": 0.8}}
        self.assertTrue(evaluate_rule(Rule.model_validate(good), context).passed)
        for expression in (
            {"not": bad},
            {"any": [good, bad]},
            {"not": {"field": "evaluation.score", "op": "eq", "value": "0.9"}},
        ):
            decision = evaluate_rule(Rule.model_validate(expression), context)
            self.assertFalse(decision.passed)
            self.assertTrue(decision.reasons)
        with self.assertRaises(ValueError):
            Rule.model_validate({"field": "candidate", "op": "python", "value": "True"})
        with self.assertRaises(ValueError):
            Rule.model_validate({"all": []})

    def test_sampling_is_stable_and_paths_preserve_json_values(self):
        self.assertEqual(
            str(create_parser().parse_args(["check"]).recipe), "recipes/rewrite/recipe.yaml"
        )
        sampler_path = self.path.parent / self.recipe.settings.source.sampler
        self.assertTrue(sampler_path.is_file())
        self.assertEqual(set(self.recipe.stages["source"].sampler.root), {"topic"})
        sampler_path.write_text("count: {type: integer, min: 3, max: 1}\n")
        with self.assertRaisesRegex(ConfigurationError, "source.sampler"):
            self.load()
        sampler_path.write_text("{}\n")
        self.assertEqual(self.load().stages["source"].sampler.root, {})
        fields = {
            "tone": Categorical(
                type="categorical",
                choices=[{"value": "formal", "weight": 2.0}, {"value": "casual", "weight": 1.0}],
            ),
            "limit": Integer(type="integer", min=1, max=5),
            "omitted": Integer(type="integer", min=1, max=1, dropout=1.0),
        }
        first = sample_fields(fields, 42, 7, "target")
        self.assertEqual(
            first, sample_fields(dict(reversed(list(fields.items()))), 42, 7, "target")
        )
        self.assertNotIn("omitted", first)
        self.assertEqual(lookup({"payload": [{"text": "hello"}]}, "payload[0].text"), "hello")
        with self.assertRaises(ValueError):
            lookup({}, "sample[*]")
