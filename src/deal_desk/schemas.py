"""The model's output contract. Every LLM response is validated against DealAssessment.

Constraints here (ranges, lengths, cross-field rules) are enforced by Pydantic after the call:
the API's structured-output schema only guarantees the JSON shape, not these rules.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(
        description="Path into the Salesforce data, e.g. opportunity.LastActivityDate, "
        "opportunity.OpportunityContactRoles[0].Contact.Name or activities[0].ActivityDate"
    )
    value: str = Field(description="The value at that path, copied exactly ('null' if empty)")


class FollowUpEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_contact_name: str | None = Field(
        description="Full name of a contact from opportunity.OpportunityContactRoles, or null if none"
    )
    subject: str = Field(min_length=3, max_length=150)
    body: str = Field(min_length=20, max_length=2000)


class DealAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opportunity_id: str
    status: Literal["healthy", "at_risk"]
    risk_reasons: list[str] = Field(description="Empty when healthy")
    confidence: float = Field(ge=0.0, le=1.0, description="How sure you are of status, 0 to 1")
    evidence: list[Evidence] = Field(min_length=1, max_length=10)
    next_step: str = Field(max_length=500, description="Concrete recommended next step for the owner")
    follow_up_email: FollowUpEmail | None = Field(
        description="Required when at_risk, unless contact_restricted is true; null when healthy")
    task_due_in_days: int = Field(ge=1, le=14)
    # Default False keeps audit rows written before this field existed valid.
    contact_restricted: bool = Field(
        default=False,
        description="True if the data says not to contact the buyer right now (for example a tender "
        "no-contact period, an agreed pause, or the customer asking for no calls). Then draft no email.")

    @model_validator(mode="after")
    def _at_risk_is_actionable(self) -> "DealAssessment":
        if self.contact_restricted and self.follow_up_email is not None:
            raise ValueError("contact_restricted is true, so follow_up_email must be null")
        if self.status == "at_risk":
            if not self.risk_reasons:
                raise ValueError("status is at_risk but risk_reasons is empty")
            if self.follow_up_email is None and not self.contact_restricted:
                raise ValueError("status is at_risk but follow_up_email is null")
            if len(self.next_step.strip()) < 10:
                raise ValueError("status is at_risk but next_step is missing")
        return self
