"""Record payloads. `extra="forbid"`: an unknown key is rejected, never dropped."""

from typing import Literal

from commissure.api import Evidence
from pydantic import BaseModel, ConfigDict

Position = Literal["support", "oppose"]
Topic = Literal["evenings", "sundays", "cost", "staffing", "students", "parking"]


class Mention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: Topic
    evidence: Evidence  # the first words in the comment that name the topic


class Comment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position: Position | None  # None only with a `no_position_phrase` warning
    position_evidence: Evidence | None
    topics: list[Mention]


class Tally(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comments: int  # comments with a record, warnings included
    support: int
    oppose: int
    no_position: int
    topics: dict[str, int]
    set_aside: list[str]  # locators of comments with no record, each for a reason below
    set_aside_reasons: dict[str, str]
