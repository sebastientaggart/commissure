"""The recipe loads through commissure's own loader, the path `commissure run` takes.

Skipped while `recipe/recipe.py` declares no stages. Add a test per stage beside this one
with the `commissure_run` fixture (skill: `build-recipe-stage`).
"""

import pytest

from recipe.recipe import STAGES


def test_recipe_loads(commissure_run):
    if not STAGES:
        pytest.skip("recipe/recipe.py declares no stages yet")
    assert commissure_run.load().stages
