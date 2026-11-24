import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from openg2p_fastapi_common.crypto import CryptoFactory
from openg2p_fastapi_common.service import BaseService
from sqlalchemy import select

from ..config import Settings
from ..db import async_session
from ..models import (
    ArtefactSource,
    ArtefactStatus,
    AuthContext,
    ConsentArtefact,
    ConsentReceipt,
    DecisionLog,
)
from ..schemas.common import ReasonCode
from ..schemas.verification import ConsentObject, Decision, ValidateRequest
from ..utils.canonical import b64url_decode, sha256_hex
from .partner_service import PartnerService
from .policy_service import PolicyService
from .receipt_service import ReceiptService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class VerificationService(BaseService):
    """The PDP hot path: verify an embedded consent object → decision."""

    def __init__(self, name="", **kwargs):
        super().__init__(name, **kwargs)
        self.partners = PartnerService.get_component()
        # Partner consent-JWS verification via the shared fastapi-common crypto
        # helper (partner-mgmt backend fetches keys from Partner Management).
        self.crypto_helper = CryptoFactory.build(backend=_config.crypto_backend)
        self.policy = PolicyService.get_component()
        self.receipts = ReceiptService.get_component()

    @staticmethod
    def _decode_jws(jws: str) -> tuple[dict, dict]:
        """Return ``(claims, protected_header)`` from a compact JWS WITHOUT
        verifying — used to identify the partner and read alg/kid before the
        signature is checked. A permit is never issued on unverified claims."""
        parts = jws.split(".")
        if len(parts) != 3:
            raise ValueError("consent_jws is not a compact JWS (expected header.payload.signature)")
        protected_header = json.loads(b64url_decode(parts[0]))
        claims = json.loads(b64url_decode(parts[1]))
        return claims, protected_header

    async def validate(self, parsed: ValidateRequest) -> Decision:
        if parsed.consent_id:
            return await self._validate_stored(parsed)
        now = datetime.now(timezone.utc)
        jws = parsed.consent_jws
        # The JWS string is itself canonical/immutable, so hash it directly.
        ctx_hash = sha256_hex(jws.encode("utf-8"))

        # Recover the claims from the JWS payload (unverified) so we can identify
        # the partner and evaluate policy. The signature is verified below,
        # before any permit is issued.
        try:
            claims, jws_header = self._decode_jws(jws)
            obj = ConsentObject(**claims)
        except Exception as exc:
            _logger.info("Malformed consent JWS: %s", exc)
            return await self._deny(
                ReasonCode.malformed_object, "consent JWS could not be decoded",
                now, ctx_hash,
            )

        # Idempotency: the same object (jti) returns its existing decision.
        existing = await self._existing_artefact(obj.jti)
        if existing is not None:
            # A jti permitted before the partner's policy required an OTP is
            # refused now, not replayed from the stored decision.
            earlier = await self.partners.get_verification_material(obj.aud)
            earlier_policy = earlier["policy"] if earlier else None
            refused = (
                self.policy.consent_method_refusal(earlier_policy, otp_confirmed=False)
                if earlier_policy else None
            )
            if refused is not None:
                return await self._deny(
                    refused.reason, refused.detail, now, ctx_hash,
                    partner_id=earlier["partner"].id, jti=obj.jti,
                    policy_version=earlier_policy.version,
                )
            return self._decision_from_artefact(existing, now)

        # 2. Known party — partner + policy (cached).
        material = await self.partners.get_verification_material(obj.aud)
        if material is None:
            return await self._deny(
                ReasonCode.unknown_partner, "partner not onboarded or suspended",
                now, ctx_hash, jti=obj.jti,
            )
        partner = material["partner"]
        policy = material["policy"]
        policy_version = policy.version if policy else None

        # 3. Signature — verify the consent JWS against the partner's key from
        # Partner Management. The shared helper reads the kid from the JWS header,
        # fetches the key for the partner's PM reference (partner_mgmt_id, falling
        # back to audience), enforces algorithm safety, and verifies.
        reference_id = partner.partner_mgmt_id or partner.audience
        alg = jws_header.get("alg")
        if policy and policy.allowed_signing_algs and alg not in policy.allowed_signing_algs:
            return await self._deny(
                ReasonCode.signature_invalid, "signing algorithm not permitted",
                now, ctx_hash, partner_id=partner.id, jti=obj.jti,
                policy_version=policy_version,
            )
        try:
            verified = await self.crypto_helper.verify_jwt(jws, km_ref_id=reference_id)
        except Exception as exc:
            _logger.warning("Consent JWS verification error: %s", exc)
            verified = False
        if not verified:
            return await self._deny(
                ReasonCode.signature_invalid,
                "signature did not verify (or no verifying key from partner management)",
                now, ctx_hash, partner_id=partner.id, jti=obj.jti,
                policy_version=policy_version,
            )

        # A partner-signed consent is not enough for an "otp" binding.
        refused = self.policy.consent_method_refusal(policy, otp_confirmed=False) if policy else None
        if refused is not None:
            return await self._deny(
                refused.reason, refused.detail, now, ctx_hash, partner_id=partner.id,
                jti=obj.jti, policy_version=policy_version,
            )

        # 10. Replay / freshness — issued_at within the configured window.
        issued_at = _aware(obj.issued_at)
        skew = timedelta(seconds=_config.replay_freshness_window_sec)
        if abs((now - issued_at).total_seconds()) > skew.total_seconds():
            return await self._deny(
                ReasonCode.replay, "issued_at outside freshness window",
                now, ctx_hash, partner_id=partner.id, jti=obj.jti,
                policy_version=policy_version,
            )

        # 4–8. Policy evaluation (audience, subject, purpose, scope, validity).
        result = self.policy.evaluate(obj, material, parsed.request_context)
        if not result.permit:
            return await self._deny(
                result.reason, result.detail, now, ctx_hash,
                partner_id=partner.id, jti=obj.jti, policy_version=result.policy_version,
            )

        # Permit — mint canonical artefact + signed receipt + decision log.
        return await self._permit(
            obj, partner, result, now, ctx_hash
        )

    # ── helpers ──────────────────────────────────────────────────────────────

    async def list_decisions(
        self,
        partner_id: Optional[str] = None,
        decision: Optional[str] = None,
        limit: int = 50,
    ) -> list:
        """Recent validation decisions (append-only log), newest first — for the
        admin console's operational/audit status view."""
        async with async_session()() as session:
            query = select(DecisionLog)
            if partner_id:
                query = query.where(DecisionLog.partner_id == partner_id)
            if decision:
                query = query.where(DecisionLog.decision == decision)
            query = query.order_by(DecisionLog.created_at.desc()).limit(min(limit, 200))
            result = await session.execute(query)
            return list(result.scalars().all())

    async def _existing_artefact(self, jti: str) -> Optional[ConsentArtefact]:
        async with async_session()() as session:
            result = await session.execute(
                select(ConsentArtefact).where(ConsentArtefact.object_jti == jti)
            )
            return result.scalars().first()

    def _decision_from_artefact(self, artefact: ConsentArtefact, now: datetime) -> Decision:
        from ..schemas.common import SubjectId

        status = artefact.status
        if status == ArtefactStatus.active.value and _aware(artefact.valid_until) < now:
            status = ArtefactStatus.expired.value
        if status == ArtefactStatus.revoked.value:
            return Decision(
                decision="deny", reason_code=ReasonCode.revoked,
                detail="consent revoked", evaluated_at=now,
            )
        if status == ArtefactStatus.expired.value:
            return Decision(
                decision="deny", reason_code=ReasonCode.expired,
                detail="consent expired", evaluated_at=now,
            )
        return Decision(
            decision="permit", reason_code=ReasonCode.ok,
            consent_id=artefact.id,
            subject_id=SubjectId(type=artefact.subject_id_type, value=artefact.subject_id_value),
            effective_data_scopes=artefact.effective_data_scopes,
            valid_until=artefact.valid_until, policy_version=artefact.policy_version,
            evaluated_at=now,
        )

    async def _permit(self, obj, partner, result, now, ctx_hash) -> Decision:
        from ..schemas.common import SubjectId

        artefact = ConsentArtefact(
            subject_id_type=obj.subject_id.type,
            subject_id_value=obj.subject_id.value,
            controller_id=partner.controller_id,
            partner_id=partner.id,
            purpose=obj.purpose,
            data_scopes=obj.data_scopes,
            effective_data_scopes=result.effective_scopes,
            fetch_type=obj.fetch_type,
            valid_from=_aware(obj.validity.valid_from),
            valid_until=_aware(obj.validity.valid_until),
            source=ArtefactSource.embedded.value,
            policy_version=result.policy_version,
            object_jti=obj.jti,
            status=ArtefactStatus.active.value,
        )
        receipt = self.receipts.build_receipt(artefact, partner)

        async with async_session()() as session:
            session.add(artefact)
            session.add(receipt)
            session.add(
                DecisionLog(
                    partner_id=partner.id, consent_id=artefact.id, object_jti=obj.jti,
                    decision="permit", reason_code=ReasonCode.ok.value,
                    policy_version=result.policy_version, request_ctx_hash=ctx_hash,
                )
            )
            await session.commit()
            await session.refresh(artefact)
            await session.refresh(receipt)

        return Decision(
            decision="permit", reason_code=ReasonCode.ok,
            consent_id=artefact.id, receipt_id=receipt.id,
            subject_id=SubjectId(type=obj.subject_id.type, value=obj.subject_id.value),
            effective_data_scopes=result.effective_scopes,
            valid_until=artefact.valid_until, policy_version=result.policy_version,
            evaluated_at=now,
        )

    async def _deny(
        self, reason: ReasonCode, detail: Optional[str], now, ctx_hash,
        partner_id: Optional[str] = None, jti: Optional[str] = None,
        policy_version: Optional[int] = None, consent_id: Optional[str] = None,
    ) -> Decision:
        async with async_session()() as session:
            session.add(
                DecisionLog(
                    partner_id=partner_id, object_jti=jti, consent_id=consent_id, decision="deny",
                    reason_code=reason.value, detail=detail,
                    policy_version=policy_version, request_ctx_hash=ctx_hash,
                )
            )
            await session.commit()
        return Decision(
            decision="deny", reason_code=reason, detail=detail, evaluated_at=now,
        )

    # ── An OTP-confirmed consent presented by its ID ─────────────────────────

    async def _validate_stored(self, parsed: ValidateRequest) -> Decision:
        """``consent_id``: a consent the subject confirmed with an OTP at the CM
        (see the partner-started OTP API). Checks the consent's state, the
        partner that obtained it and the subject; the scopes are narrowed to the
        partner's CURRENT policy, and the policy's other terms apply as they are
        now. Any other stored consent is not presentable here (unknown_consent)."""
        from ..schemas.common import SubjectId

        now = datetime.now(timezone.utc)
        consent_id = parsed.consent_id
        partner_aud = parsed.consent_partner_id
        ctx_hash = sha256_hex(f"consent_id:{consent_id}:{partner_aud or ''}".encode("utf-8"))

        async with async_session()() as session:
            artefact = await session.get(ConsentArtefact, consent_id)
            ctx = (
                await session.get(AuthContext, artefact.auth_context_id)
                if artefact is not None and artefact.auth_context_id
                else None
            )
        if (
            artefact is None
            or artefact.source != ArtefactSource.originated.value
            or ctx is None
            or ctx.auth_method != "otp"
        ):
            return await self._deny(
                ReasonCode.unknown_consent, "no OTP-confirmed consent with this consent_id",
                now, ctx_hash,
            )
        log = {"partner_id": artefact.partner_id, "consent_id": artefact.id}
        if artefact.status == ArtefactStatus.revoked.value:
            return await self._deny(ReasonCode.revoked, "consent revoked", now, ctx_hash, **log)
        if artefact.status == ArtefactStatus.expired.value or _aware(artefact.valid_until) < now:
            return await self._deny(ReasonCode.expired, "consent expired", now, ctx_hash, **log)
        if artefact.status != ArtefactStatus.active.value:
            return await self._deny(
                ReasonCode.unknown_consent, f"consent is '{artefact.status}'", now, ctx_hash, **log
            )
        if _aware(artefact.valid_from) > now:
            return await self._deny(ReasonCode.expired, "not yet valid", now, ctx_hash, **log)

        # The presenting partner must be the one the consent was given to; the
        # partner is looked up by the consent's binding, never by the request.
        material = (
            await self.partners.get_verification_material(partner_aud) if partner_aud else None
        )
        if material is None or material["partner"].id != artefact.partner_id:
            return await self._deny(
                ReasonCode.partner_mismatch,
                "consent_partner_id is not the partner the consent was given to",
                now, ctx_hash, **log,
            )
        policy = material["policy"]
        if policy is None:
            return await self._deny(
                ReasonCode.unknown_partner, "No active policy", now, ctx_hash, **log
            )
        ctx_subject = parsed.request_context.subject_id if parsed.request_context else None
        if (
            ctx_subject is not None
            and ctx_subject.type == artefact.subject_id_type
            and ctx_subject.value != artefact.subject_id_value
        ):
            return await self._deny(
                ReasonCode.subject_mismatch,
                "request_context.subject_id does not match the consent subject",
                now, ctx_hash, **log,
            )

        # The current policy's terms apply to the stored consent as they do to a signed one.
        refusal = self.policy.consent_method_refusal(
            policy, otp_confirmed=True
        ) or self.policy.terms_refusal(
            policy, subject_id_type=artefact.subject_id_type, purpose=artefact.purpose,
            valid_from=artefact.valid_from, valid_until=artefact.valid_until,
        )
        if refusal is not None:
            return await self._deny(
                refusal.reason, refusal.detail, now, ctx_hash,
                policy_version=refusal.policy_version, **log,
            )
        effective = set(artefact.effective_data_scopes or []) & set(
            policy.allowed_data_scopes or []
        )
        requested = parsed.request_context.requested_scopes if parsed.request_context else None
        if requested:
            effective &= set(requested)
        if not effective:
            return await self._deny(
                ReasonCode.scope_exceeds_policy,
                "no requested scope is permitted by policy", now, ctx_hash,
                policy_version=policy.version, **log,
            )

        async with async_session()() as session:
            receipt = (
                await session.execute(
                    select(ConsentReceipt).where(ConsentReceipt.consent_id == artefact.id)
                )
            ).scalars().first()
            session.add(
                DecisionLog(
                    partner_id=artefact.partner_id, consent_id=artefact.id,
                    decision="permit", reason_code=ReasonCode.ok.value,
                    policy_version=policy.version, request_ctx_hash=ctx_hash,
                )
            )
            await session.commit()
        return Decision(
            decision="permit", reason_code=ReasonCode.ok,
            consent_id=artefact.id, receipt_id=receipt.id if receipt else None,
            subject_id=SubjectId(type=artefact.subject_id_type, value=artefact.subject_id_value),
            effective_data_scopes=sorted(effective),
            valid_until=artefact.valid_until, policy_version=policy.version,
            evaluated_at=now,
        )
