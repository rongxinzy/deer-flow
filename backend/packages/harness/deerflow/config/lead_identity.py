"""Deployment-scoped lead-agent identity metadata.

These labels can originate with business users. They are rendered as data in a
HumanMessage, never interpolated into the system prompt.
"""

from pydantic import BaseModel, ConfigDict, Field


class LeadIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment_id: str = Field(min_length=1, max_length=128)
    deployment_name: str = Field(default="", max_length=200)
    employee_name: str = Field(default="", max_length=200)
    purpose: str = Field(default="", max_length=500)
    team_id: str = Field(default="", max_length=128)
    team_name: str = Field(default="", max_length=200)
    owner_id: str = Field(default="", max_length=128)
    owner_name: str = Field(default="", max_length=200)
