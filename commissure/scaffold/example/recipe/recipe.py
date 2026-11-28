"""Entry point: `commissure run --step <stage>` loads this file from the solution folder.

Two stages over the `comment` role. Each result is checkable against its original: the
position and every topic carry an exact span of the comment's text, which core verifies when
the record is stored. `Check`s are the questions a reviewer answers on a random sample
(`commissure review`, or `/review` in `commissure serve`).
"""

from commissure.api import Check, Stage

from .extract import extract
from .interface import INTERFACE
from .schema import Comment, Tally
from .tally import tally

STAGES = [
    Stage(
        "extract",
        extract,
        schema=Comment,
        text="default",
        checks=(
            Check("/position_evidence", "Does this passage state the recorded position?"),
            Check("/topics/*/evidence", "Does this passage raise the recorded topic?"),
        ),
    ),
    Stage("tally", tally, schema=Tally, subject="run", needs=("extract",)),
]

__all__ = ["INTERFACE", "STAGES"]
