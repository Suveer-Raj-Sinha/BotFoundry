"""The Rulebook document (one JSON file per bot, versioned on save)."""
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..workflow.definition import ID_PATTERN
from .permissions import Policy


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_files_per_run: int | None = Field(default=None, ge=1, le=1_000_000)
    max_run_seconds: float | None = Field(default=None, gt=0, le=86_400)


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeout_seconds: int = Field(default=300, ge=5, le=86_400)  # no answer in this time = denied


class Rulebook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    bot_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    allowed_folders: list[str] = Field(default_factory=list)
    forbidden_folders: list[str] = Field(default_factory=list)
    default_policy: Policy = Policy.DENY  # for permissions not listed below
    permissions: dict[str, Policy] = Field(default_factory=dict)
    limits: Limits = Field(default_factory=Limits)
    approval: Approval = Field(default_factory=Approval)

    def canonical_json(self) -> str:
        """Stable text form; two Rulebooks with the same content always produce the same string."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
