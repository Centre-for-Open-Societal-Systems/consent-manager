from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import BaseORMModelWithId


class ArtefactStatus(str, Enum):
    active = "active"
    revoked = "revoked"
    expired = "expired"


class ArtefactSource(str, Enum):
    embedded = "embedded"  # partner-signed object, verified on the hot path
    originated = "originated"  # collected by the CM via the lifecycle flow


class RequestStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    denied = "denied"
    expired = "expired"
    # An OTP request closed by the CM: out of wrong codes, or its code could not be delivered.
    cancelled = "cancelled"


class RequestMethod(str, Enum):
    """How a consent request is granted. NULL (every request of the origination flow) is
    the subject's own login; ``otp`` is raised by a partner and granted by a one-time code."""

    otp = "otp"


class ConsentRequest(BaseORMModelWithId):
    """Origination flow — a pending request before the subject authenticates."""

    __tablename__ = "consent_requests"

    subject_id_type: Mapped[str] = mapped_column(String(50), index=True)
    subject_id_value: Mapped[str] = mapped_column(String(255), index=True)
    controller_id: Mapped[str] = mapped_column(String(255), index=True)
    partner_id: Mapped[str] = mapped_column(String, index=True)
    purpose: Mapped[dict] = mapped_column(JSONB)
    requested_scopes: Mapped[list] = mapped_column(JSONB, default=list)
    valid_from: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=RequestStatus.pending.value, index=True)

    # Partner-started OTP requests only (NULL for the origination flow).
    method: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    verification_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # The code is stored only as a salted hash. A transaction is the request itself.
    otp_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    otp_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    otp_attempts: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=0)
    otp_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    otp_channel: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    otp_destination: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    otp_provider: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    otp_reference: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Across every code issued for the request; a resend does not reset them.
    otp_send_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=0)
    otp_last_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    otp_failures_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=0)


class AuthContext(BaseORMModelWithId):
    """Built from a validated OIDC ID token. The raw token is never stored."""

    __tablename__ = "auth_contexts"

    consent_request_id: Mapped[str] = mapped_column(String, index=True)
    auth_provider: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    auth_method: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    auth_timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    issuer: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    id_token_hash: Mapped[str] = mapped_column(String(128))
    token_validated: Mapped[bool] = mapped_column(default=False)
    verified_claims: Mapped[dict] = mapped_column(JSONB, default=dict)


class ConsentArtefact(BaseORMModelWithId):
    """Canonical consent decision — from an embedded object or an origination."""

    __tablename__ = "consent_artefacts"

    subject_id_type: Mapped[str] = mapped_column(String(50), index=True)
    subject_id_value: Mapped[str] = mapped_column(String(255), index=True)
    controller_id: Mapped[str] = mapped_column(String(255), index=True)
    partner_id: Mapped[str] = mapped_column(String, index=True)

    purpose: Mapped[dict] = mapped_column(JSONB)
    data_scopes: Mapped[list] = mapped_column(JSONB, default=list)
    effective_data_scopes: Mapped[list] = mapped_column(JSONB, default=list)
    fetch_type: Mapped[str] = mapped_column(String(20), default="oneshot")

    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    source: Mapped[str] = mapped_column(String(20), default=ArtefactSource.embedded.value)
    policy_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    auth_context_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Idempotency / replay: the jti of the embedded object that produced this
    # artefact. Unique so the same object never mints duplicates.
    object_jti: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default=ArtefactStatus.active.value, index=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ConsentReceipt(BaseORMModelWithId):
    """Kantara/ISO-27560-aligned receipt, signed by the CM private key."""

    __tablename__ = "consent_receipts"

    consent_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    artefact_hash: Mapped[str] = mapped_column(String(128))
    algorithm: Mapped[str] = mapped_column(String(20))
    kid: Mapped[str] = mapped_column(String(255))
    signature: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(String(16), default="1.1")
    # The full signed receipt document (JSON-LD) for retrieval.
    document: Mapped[dict] = mapped_column(JSONB)


class RevocationRecord(BaseORMModelWithId):
    """Append-only record of a revocation event."""

    __tablename__ = "revocation_records"

    consent_id: Mapped[str] = mapped_column(String, index=True)
    originated_by: Mapped[str] = mapped_column(String(20))  # subject | controller | partner
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
