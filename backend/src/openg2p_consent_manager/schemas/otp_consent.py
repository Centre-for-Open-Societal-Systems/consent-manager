from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from .common import SubjectId


class OtpValidity(BaseModel):
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None


class OtpRequestCreate(BaseModel):
    """A partner-started consent request that the subject confirms with a one-time
    code. The partner is the caller's own binding (from its token), never a field."""

    method: Literal["otp"]
    subject_id: SubjectId
    purpose: Dict[str, Any] = Field(..., examples=[{"code": "credit-assessment"}])
    data_scopes: List[str] = Field(..., min_length=1)
    validity: Optional[OtpValidity] = None


class OtpRequestResponse(BaseModel):
    id: str
    status: str
    method: str
    created_by: Optional[str] = None
    subject_id: SubjectId
    purpose: Dict[str, Any]
    data_scopes: List[str]
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    # Set once a correct code has granted the consent.
    consent_id: Optional[str] = None
    otp_channel: Optional[str] = None
    otp_expires_at: Optional[datetime] = None
    otp_attempts_left: Optional[int] = None
    created_at: datetime


class VerifyOtpRequest(BaseModel):
    otp: str = Field(..., min_length=4, max_length=10)


class OtpConsentGranted(BaseModel):
    """The consent created by a correct code; ``consent_id`` is what the partner
    presents (``/validate`` with ``consent_id``)."""

    consent_id: str
    consent_request_id: str
    receipt_id: Optional[str] = None
    status: str
    auth_method: str = "otp"
    subject_id: SubjectId
    purpose: Dict[str, Any]
    data_scopes: List[str]
    effective_data_scopes: List[str]
    valid_from: datetime
    valid_until: datetime
    policy_version: Optional[int] = None
