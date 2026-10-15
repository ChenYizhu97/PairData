"""Small transport fake and temporary recipe fixtures for offline checks."""

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage

from pairdata.recipe import load_recipe


class RecipeFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.env_file = self.root / "test.env"
        self.env_file.write_text(
            "MODEL_NAME=fake\nOPENAI_API_KEY=test-secret\nOPENAI_BASE_URL=http://127.0.0.1:1/v1\n"
        )
        self.path = self.root / "recipe" / "recipe.yaml"
        shutil.copytree(
            Path(__file__).resolve().parents[1] / "recipes" / "rewrite", self.path.parent
        )
        self.recipe = self.load()

    def load(self):
        return load_recipe(self.path, {"retry_initial": 0.0, "retry_max": 0.0}, self.env_file)


def stage_of(messages):
    return json.loads(messages[0]["content"].rsplit("without Markdown:\n", 1)[1])["title"]


def response(stage, value=None):
    if value is None:
        value = (
            {"text": "The meeting starts at 10."}
            if stage.endswith("generate")
            else {"acceptable": True, "reason": "Facts and requested tone preserved."}
        )
    return AIMessage(
        content=json.dumps(value),
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )
