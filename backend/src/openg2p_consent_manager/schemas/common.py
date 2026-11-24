from datetime import datetime
from enum import Enum
from typing import Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field


class ReasonCode(str, Enum):
    ok = "ok"
    malformed_object = "malformed_object"
    unknown_partner = "unknown_partner"
    signature_invalid = "signature_invalid"
    audience_mismatch = "audience_mismatch"
    subject_not_allowed = "subject_not_allowed"
    purpose_not_allowed = "purpose_not_allowed"
    scope_exceeds_policy = "scope_exceeds_policy"
    validity_exceeds_policy = "validity_exceeds_policy"
    # The policy's consent_method does not accept this kind of consent (e.g.
    # a partner-signed consent_jws for an "otp" partner).
    consent_method_not_allowed = "consent_method_not_allowed"
    # A stored consent presented by consent_id: unknown, or not the presenting
    # partner's, or about another subject than the request.
    unknown_consent = "unknown_consent"
    partner_mismatch = "partner_mismatch"
    subject_mismatch = "subject_mismatch"
    expired = "expired"
    revoked = "revoked"
    replay = "replay"


class SubjectId(BaseModel):
    type: str = Field(..., examples=["national_id"])
    value: str = Field(..., examples=["FARMER_1234"])


class Problem(BaseModel):
    """Standard error body for non-decision endpoints."""

    error: str
    detail: Optional[str] = None
    trace_id: Optional[str] = None


DataT = TypeVar("DataT")


class Paginated(BaseModel, Generic[DataT]):
    items: List[DataT]
    total: int
    page: int
    size: int
    pages: int


class StatusResponse(BaseModel):
    consent_id: str
    status: str
    valid_until: Optional[datetime] = None
    checked_at: datetime
