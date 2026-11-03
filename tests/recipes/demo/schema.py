from pydantic import BaseModel, ConfigDict

from commissure.api import Evidence


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    amount: int


class Score(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    doubled: int


class Total(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    count: int
    total: int
    excluded: list[str]


class Cited(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote: str
    evidence: list[Evidence]


class Carried(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence: list[Evidence]
