"""The recipe loads through commissure's own loader, the path `commissure run` takes.

Skipped while `recipe/recipe.py` declares no stages. Add a test per stage beside this one
(skill: `build-recipe-stage`).
"""

from pathlib import Path

import pytest
from commissure.recipes import load_recipe

from recipe.recipe import STAGES

RECIPE = Path(__file__).resolve().parents[1] / "recipe"


def test_recipe_loads():
    if not STAGES:
        pytest.skip("recipe/recipe.py declares no stages yet")
    assert load_recipe(RECIPE).stages
