import logging
from typing import Optional

from fastapi import Depends
from fastapi.responses import JSONResponse
from openg2p_fastapi_common.controller import BaseController

from ..auth import PartnerUser, current_partner_user
from ..config import Settings
from ..models import ConsentArtefact, ConsentRequest
from ..schemas.common import SubjectId
from ..schemas.otp_consent import (
    OtpConsentGranted,
    OtpRequestCreate,
    OtpRequestResponse,
    VerifyOtpRequest,
)
from ..services import ConsentService, LifecycleError, OtpConsentService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": detail})


def _not_found() -> JSONResponse:
    return _error(404, "not_found")


def _request_view(req: ConsentRequest, consent_id: Optional[str]) -> OtpRequestResponse:
    pending = req.status == "pending" and req.otp_hash is not None
    return OtpRequestResponse(
        id=req.id,
        status=req.status,
        method=req.method,
        created_by=req.created_by,
        subject_id=SubjectId(type=req.subject_id_type, value=req.subject_id_value),
        purpose=req.purpose,
        data_scopes=list(req.requested_scopes or []),
        valid_from=req.valid_from,
        valid_until=req.valid_until,
        consent_id=consent_id,
        otp_channel=req.otp_channel,
        otp_expires_at=req.otp_expires_at,
        otp_attempts_left=(
            max(0, _config.otp_max_attempts - (req.otp_attempts or 0)) if pending else None
        ),
        created_at=req.created_at,
    )


class OtpConsentController(BaseController):
    """Partner-started OTP consent — partner users from the Keycloak ``partner``
    realm. Every call is scoped to the token's ``partner_id``; another partner's
    request is 404. Disabled (404) while the partner realm is not configured."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.otp_consents = OtpConsentService.get_component()
        self.consents = ConsentService.get_component()
        self.router.prefix += "/consent/v1/partner-portal"
        self.router.tags += ["Partner OTP consent"]

        def route(path, endpoint, method, model=None, status_code=200):
            self.router.add_api_route(
                path, endpoint, methods=[method], status_code=status_code,
                responses={status_code: {"model": model}} if model else None,
            )

        route("/consent-requests", self.create_request, "POST", OtpRequestResponse, 201)
        route("/consent-requests/{request_id}", self.get_request, "GET", OtpRequestResponse)
        # The subject's one-time code (sent when the request was raised) grants the consent.
        route(
            "/consent-requests/{request_id}/verify-otp", self.verify_otp, "POST",
            OtpConsentGranted, 201,
        )
        route(
            "/consent-requests/{request_id}/resend-otp", self.resend_otp, "POST",
            OtpRequestResponse,
        )

    async def create_request(
        self, data: OtpRequestCreate, user: PartnerUser = Depends(current_partner_user),
    ):
        try:
            req = await self.otp_consents.create_request(user.partner_id, user.username, data)
        except LifecycleError as exc:
            return _error(exc.status_code, exc.detail)
        return JSONResponse(
            status_code=201, content=_request_view(req, None).model_dump(mode="json"),
        )

    async def get_request(
        self, request_id: str, user: PartnerUser = Depends(current_partner_user)
    ):
        req = await self.otp_consents.get_request(request_id, user.partner_id)
        if req is None:
            return _not_found()
        return _request_view(req, await self.otp_consents.consent_id(req.id))

    async def verify_otp(
        self, request_id: str, data: VerifyOtpRequest,
        user: PartnerUser = Depends(current_partner_user),
    ):
        try:
            req, artefact = await self.otp_consents.verify_otp(
                request_id, user.partner_id, user.username, data.otp
            )
        except LifecycleError as exc:
            return _error(exc.status_code, exc.detail)
        return await self._granted(req, artefact)

    async def _granted(self, req: ConsentRequest, artefact: ConsentArtefact):
        receipt = await self.consents.get_receipt_by_consent(artefact.id)
        body = OtpConsentGranted(
            consent_id=artefact.id,
            consent_request_id=req.id,
            receipt_id=receipt.id if receipt else None,
            status=artefact.status,
            subject_id=SubjectId(type=artefact.subject_id_type, value=artefact.subject_id_value),
            purpose=artefact.purpose,
            data_scopes=list(artefact.data_scopes or []),
            effective_data_scopes=list(artefact.effective_data_scopes or []),
            valid_from=artefact.valid_from,
            valid_until=artefact.valid_until,
            policy_version=artefact.policy_version,
        )
        return JSONResponse(status_code=201, content=body.model_dump(mode="json"))

    async def resend_otp(self, request_id: str, user: PartnerUser = Depends(current_partner_user)):
        try:
            req = await self.otp_consents.resend_otp(request_id, user.partner_id)
        except LifecycleError as exc:
            return _error(exc.status_code, exc.detail)
        return _request_view(req, None)
