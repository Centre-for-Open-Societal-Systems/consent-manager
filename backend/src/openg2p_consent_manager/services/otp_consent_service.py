"""Partner-started OTP consent: the partner raises a consent request, the CM sends
the subject a one-time code at once, the subject tells the partner the code and
the partner submits it. A correct code grants the consent immediately.

The request is the partner's own (scoped by its binding; another partner's
request is "not found"), it is checked against the binding's policy when raised
and again, as the policy is *then*, when the code is verified. The consent that
results is an ordinary stored artefact with a signed receipt, and its
``AuthContext`` records ``auth_method = "otp"``: that is what a binding whose
``consent_method`` is ``otp`` requires at /validate. It is presented there by its
``consent_id``.
"""
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from openg2p_fastapi_common.service import BaseService
from sqlalchemy import func, select

from ..config import Settings
from ..db import async_session
from ..models import (
    ArtefactSource,
    ArtefactStatus,
    AuthContext,
    ConsentArtefact,
    ConsentRequest,
    Partner,
    PartnerStatus,
    RequestMethod,
    RequestStatus,
)
from ..utils.canonical import iso_duration_to_timedelta
from .lifecycle_service import LifecycleError
from .otp_service import UNCERTAIN_DELIVERY, OtpError, OtpService
from .partner_service import PartnerService
from .policy_service import PolicyService
from .receipt_service import ReceiptService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

_DEFAULT_VALIDITY = timedelta(days=365)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _max_validity(policy) -> Optional[timedelta]:
    if not policy.max_validity_duration:
        return None
    try:
        return iso_duration_to_timedelta(policy.max_validity_duration)
    except ValueError:
        return None


class OtpConsentService(BaseService):
    def __init__(self, name="", **kwargs):
        super().__init__(name, **kwargs)
        self.partners = PartnerService.get_component()
        self.receipts = ReceiptService.get_component()
        self.policy = PolicyService.get_component()

    @property
    def otp(self) -> OtpService:
        return OtpService.get_component()

    @staticmethod
    async def _binding(audience: str) -> Optional[Partner]:
        """The caller's active binding, by the audience on its token. Read from the
        database, not the hot-path cache."""
        async with async_session()() as session:
            return (
                await session.execute(
                    select(Partner).where(
                        Partner.audience == audience,
                        Partner.status == PartnerStatus.active.value,
                    )
                )
            ).scalars().first()

    @staticmethod
    def _visible(req: Optional[ConsentRequest], binding: Optional[Partner]) -> bool:
        """An OTP request, and the caller's own."""
        return (
            req is not None
            and binding is not None
            and req.method == RequestMethod.otp.value
            and req.partner_id == binding.id
        )

    async def create_request(self, audience: str, username: str, data) -> ConsentRequest:
        """Raise the request (checked against the partner's policy) and send the
        subject the code."""
        binding = await self._binding(audience)
        if binding is None:
            raise LifecycleError(422, "no active partner binding")
        policy = await self.partners.get_policy(binding.id)
        if policy is None:
            raise LifecycleError(422, "no active policy")
        if not set(data.data_scopes).issubset(set(policy.allowed_data_scopes or [])):
            raise LifecycleError(422, "scope_exceeds_policy")

        now = datetime.now(timezone.utc)
        validity = data.validity
        valid_from = _aware(validity.valid_from) if validity and validity.valid_from else None
        valid_until = _aware(validity.valid_until) if validity and validity.valid_until else None
        if valid_until is not None and valid_until <= now:
            raise LifecycleError(422, "validity.valid_until is in the past")
        refused = self.policy.terms_refusal(
            policy, subject_id_type=data.subject_id.type, purpose=data.purpose,
            valid_from=valid_from or now, valid_until=valid_until or (valid_from or now),
        )
        if refused is not None:
            raise LifecycleError(422, refused.reason.value)

        if self.otp.readiness_error():  # refused before anything is stored
            raise LifecycleError(503, "otp_unavailable")
        await self._check_subject_budget(data.subject_id.value)

        async with async_session()() as session:
            req = ConsentRequest(
                subject_id_type=data.subject_id.type,
                subject_id_value=data.subject_id.value,
                controller_id=binding.controller_id,
                partner_id=binding.id,
                purpose=data.purpose,
                requested_scopes=sorted(set(data.data_scopes)),
                valid_from=valid_from,
                valid_until=valid_until,
                status=RequestStatus.pending.value,
                method=RequestMethod.otp.value,
                created_by=username,
            )
            session.add(req)
            try:
                await self.otp.issue(req, req.subject_id_value)
            except OtpError as exc:
                req.status = RequestStatus.cancelled.value
                await session.commit()
                raise LifecycleError(exc.status, exc.reason) from exc
            await session.commit()
            await session.refresh(req)
        _logger.info("OTP consent request %s raised by %s (%s); code sent",
                     req.id, audience, username)
        return req

    @staticmethod
    async def _check_subject_budget(subject_id_value: str) -> None:
        """Codes sent to one subject, by any partner, within the window: the
        sends of every OTP request that had a code sent in it (an over-count,
        never an under-count)."""
        since = datetime.now(timezone.utc) - timedelta(seconds=_config.otp_subject_window_sec)
        async with async_session()() as session:
            sent = (
                await session.execute(
                    select(func.coalesce(func.sum(ConsentRequest.otp_send_count), 0)).where(
                        ConsentRequest.method == RequestMethod.otp.value,
                        ConsentRequest.subject_id_value == subject_id_value,
                        ConsentRequest.otp_last_sent_at >= since,
                    )
                )
            ).scalar_one()
        if sent >= _config.otp_max_sends_per_subject:
            raise LifecycleError(429, "subject_otp_limit_reached")

    async def get_request(self, request_id: str, audience: str) -> Optional[ConsentRequest]:
        binding = await self._binding(audience)
        async with async_session()() as session:
            req = await session.get(ConsentRequest, request_id)
        return req if self._visible(req, binding) else None

    async def consent_id(self, request_id: str) -> Optional[str]:
        """The consent a correct code granted for the request, if any."""
        async with async_session()() as session:
            return (
                await session.execute(
                    select(ConsentArtefact.id)
                    .join(AuthContext, AuthContext.id == ConsentArtefact.auth_context_id)
                    .where(
                        AuthContext.consent_request_id == request_id,
                        AuthContext.auth_method == "otp",
                    )
                )
            ).scalars().first()

    async def resend_otp(self, request_id: str, audience: str) -> ConsentRequest:
        """A new code for a still-pending request (the old one stops working)."""
        binding = await self._binding(audience)
        async with async_session()() as session:
            req = await session.get(ConsentRequest, request_id, with_for_update=True)
            if not self._visible(req, binding):
                raise LifecycleError(404, "not_found")
            if req.status != RequestStatus.pending.value:
                raise LifecycleError(409, f"request is '{req.status}'")
            # A new code resets the attempts for that code only; these budgets
            # are what stop resends from undoing a lockout.
            now = datetime.now(timezone.utc)
            last = _aware(req.otp_last_sent_at) if req.otp_last_sent_at else None
            if last is not None and now < last + timedelta(seconds=_config.otp_resend_cooldown_sec):
                raise LifecycleError(429, "resend_too_soon")
            if (req.otp_send_count or 0) >= _config.otp_max_sends_per_request:
                raise LifecycleError(429, "resend_limit_reached")
            await self._check_subject_budget(req.subject_id_value)
            try:
                await self.otp.issue(req, req.subject_id_value)
            except OtpError as exc:
                if exc.reason == UNCERTAIN_DELIVERY:
                    # The code may still arrive: keep it valid and keep the send counted.
                    await session.commit()
                raise LifecycleError(exc.status, exc.reason) from exc
            await session.commit()
            await session.refresh(req)
            return req

    async def verify_otp(
        self, request_id: str, audience: str, username: str, code: str
    ) -> tuple[ConsentRequest, ConsentArtefact]:
        """Check the code and, if it is right, create the consent: the requested
        scopes narrowed to the binding's policy as it is now."""
        binding = await self._binding(audience)
        async with async_session()() as session:
            req = await session.get(ConsentRequest, request_id, with_for_update=True)
            if not self._visible(req, binding):
                raise LifecycleError(404, "not_found")
            if req.status != RequestStatus.pending.value:
                raise LifecycleError(409, f"request is '{req.status}'")
            try:
                await self.otp.verify(req, code)
            except OtpError as exc:
                status, reason = exc.status, exc.reason
                if (req.otp_failures_total or 0) >= _config.otp_max_failures_per_request:
                    # Out of guesses over all its codes: the request is closed
                    # and the partner must raise a new one.
                    req.status = RequestStatus.cancelled.value
                    req.verification_note = "OTP failure limit reached"
                    _logger.warning("OTP consent request %s cancelled: failure limit", req.id)
                    status, reason = 429, "otp_failure_limit_reached"
                await session.commit()  # keep the attempt counts
                raise LifecycleError(status, reason) from exc

            now = datetime.now(timezone.utc)
            valid_from = _aware(req.valid_from) if req.valid_from else now
            # The whole current policy applies, not only its scopes: a request whose
            # purpose, subject id type or requested validity the policy no longer
            # allows is refused. Without an explicit end the validity is derived
            # from the policy below, so it always fits.
            until = _aware(req.valid_until) if req.valid_until else valid_from
            partner = await session.get(Partner, req.partner_id)
            policy = (
                await self.partners.get_policy(partner.id)
                if partner is not None and partner.status == PartnerStatus.active.value
                else None
            )
            refusal = None
            if policy is None:
                refusal = "no active policy"
            else:
                refused = self.policy.terms_refusal(
                    policy, subject_id_type=req.subject_id_type, purpose=req.purpose,
                    valid_from=valid_from, valid_until=until,
                )
                if refused is not None:
                    refusal = refused.reason.value
            effective = (
                sorted(set(req.requested_scopes or []) & set(policy.allowed_data_scopes or []))
                if refusal is None
                else []
            )
            if refusal is not None or not effective:
                await session.commit()  # the code is spent either way
                raise LifecycleError(
                    422, refusal or "no requested scope is permitted by the partner's current policy"
                )

            valid_until = (
                _aware(req.valid_until) if req.valid_until
                else valid_from + (_max_validity(policy) or _DEFAULT_VALIDITY)
            )
            if valid_until <= now:
                await session.commit()
                raise LifecycleError(409, "the requested validity has already ended")

            ctx = AuthContext(
                consent_request_id=req.id,
                auth_provider=req.otp_provider or "otp",
                auth_method="otp",
                auth_timestamp=req.otp_verified_at or now,
                issuer=req.otp_provider or "otp",
                # No ID token in this path; the column is not nullable, so it
                # holds a hash naming the act. token_validated stays False.
                id_token_hash=hashlib.sha256(f"otp:{req.id}".encode("utf-8")).hexdigest(),
                token_validated=False,
                verified_claims={
                    "auth_method": "otp",
                    "otp_provider": req.otp_provider,
                    "otp_channel": req.otp_channel,
                    "otp_reference": req.otp_reference,
                    "subject_id_value": req.subject_id_value,
                    "submitted_by": username,
                },
            )
            session.add(ctx)
            await session.flush()

            artefact = ConsentArtefact(
                subject_id_type=req.subject_id_type,
                subject_id_value=req.subject_id_value,
                controller_id=partner.controller_id,
                partner_id=partner.id,
                purpose=req.purpose,
                data_scopes=req.requested_scopes,
                effective_data_scopes=effective,
                fetch_type=policy.fetch_type or "oneshot",
                valid_from=valid_from,
                valid_until=valid_until,
                source=ArtefactSource.originated.value,
                policy_version=policy.version,
                auth_context_id=ctx.id,
                status=ArtefactStatus.active.value,
                created_at=now,
            )
            receipt = self.receipts.build_receipt(artefact, partner)
            req.status = RequestStatus.approved.value
            req.verified_at = now
            session.add(artefact)
            session.add(receipt)
            await session.commit()
            await session.refresh(req)
            await session.refresh(artefact)
        _logger.info("OTP consent request %s: code verified, consent %s granted",
                     request_id, artefact.id)
        return req, artefact


__all__ = ["OtpConsentService"]
