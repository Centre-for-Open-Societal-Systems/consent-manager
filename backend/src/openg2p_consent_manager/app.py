# ruff: noqa: E402
import asyncio
import logging

from .config import Settings

_config = Settings.get_config()

from openg2p_fastapi_common.app import Initializer as BaseInitializer

from .controllers import (
    AweController,
    DecisionsController,
    LifecycleController,
    OtpConsentController,
    PartnerController,
    SubjectController,
    VerificationController,
    WellKnownController,
)
from .models import (
    AuditLog,
    AuthContext,
    AweProcessedEvent,
    ConsentArtefact,
    ConsentReceipt,
    ConsentRequest,
    DecisionLog,
    Partner,
    PartnerPolicy,
    RevocationRecord,
)
from .services import (
    AweClient,
    AweWebhookService,
    ConsentService,
    CryptoService,
    LifecycleService,
    OtpConsentService,
    OtpService,
    PartnerService,
    PolicyService,
    ReceiptService,
    VerificationService,
)

_logger = logging.getLogger(_config.logging_default_logger_name)


class Initializer(BaseInitializer):
    def initialize(self, **kwargs):
        super().initialize(**kwargs)

        # Services — order matters: a service that calls get_component() in its
        # __init__ must be constructed after its dependencies.
        CryptoService()
        PartnerService()
        PolicyService()
        ReceiptService()
        VerificationService()
        ConsentService()
        LifecycleService()
        OtpService()
        OtpConsentService()  # depends on PartnerService, ReceiptService, PolicyService
        AweClient()
        AweWebhookService()  # depends on PartnerService

        # Controllers — mounted per API audience (the platform's 4-API pattern).
        # One image, one deployable per audience; each mounts only its routes.
        audience = _config.api_audience
        staff = audience in ("staff", "all")
        partner = audience in ("partner", "all")
        beneficiary = audience in ("beneficiary", "all")
        _logger.info("Consent Manager API audience: %s", audience)

        if partner:
            # PARTNER api — PDP. Trust = partner-signed consent object (PM keys),
            # no Keycloak. Serves /validate, status, receipts, JWKS.
            VerificationController().post_init()
            WellKnownController().post_init()
        if staff:
            # STAFF api — Keycloak staff realm. Policy admin, approvals, decisions.
            PartnerController().post_init()
            AweController().post_init()
            DecisionsController().post_init()
            # Partner users (partner realm, own token check) raise OTP consent
            # requests; the API answers 404 until the partner realm is configured.
            OtpConsentController().post_init()
        if beneficiary:
            # BENEFICIARY api — Keycloak beneficiary realm. /my/* + origination.
            SubjectController().post_init()
            LifecycleController().post_init()

    def migrate_database(self, args):
        super().migrate_database(args)

        async def migrate():
            _logger.info("Migrating consent manager database")
            for model in (
                Partner,
                PartnerPolicy,
                ConsentRequest,
                AuthContext,
                ConsentArtefact,
                ConsentReceipt,
                RevocationRecord,
                DecisionLog,
                AuditLog,
                AweProcessedEvent,
            ):
                await model.create_migrate()

            # create_migrate() only creates missing tables; it does not ALTER an
            # existing one. Apply idempotent column changes so a DB created under
            # an earlier schema picks up the current shape on next migrate.
            from openg2p_fastapi_common.context import dbengine
            from sqlalchemy import text

            async with dbengine.get().begin() as conn:
                # Partner is now a policy binding: keys + identity live in Partner
                # Management. Carry a PM reference; identity/onboarding fields go.
                await conn.execute(
                    text(
                        "ALTER TABLE partners ADD COLUMN IF NOT EXISTS "
                        "partner_mgmt_id VARCHAR(255)"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_partners_partner_mgmt_id "
                        "ON partners (partner_mgmt_id)"
                    )
                )
                # name is now an optional display label; org_name + the old
                # partner-onboarding approval columns are gone (approval moved
                # onto the policy version).
                await conn.execute(text("ALTER TABLE partners ALTER COLUMN name DROP NOT NULL"))
                await conn.execute(text("ALTER TABLE partners DROP COLUMN IF EXISTS org_name"))
                await conn.execute(text("ALTER TABLE partners DROP COLUMN IF EXISTS approval_status"))
                await conn.execute(text("ALTER TABLE partners DROP COLUMN IF EXISTS awe_request_id"))
                # AWE approval now correlates to a policy version.
                await conn.execute(
                    text(
                        "ALTER TABLE partner_policies ADD COLUMN IF NOT EXISTS "
                        "awe_request_id VARCHAR(64)"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_partner_policies_awe_request_id "
                        "ON partner_policies (awe_request_id)"
                    )
                )
                # How the subject's consent must arrive (NULL = partner_signed).
                await conn.execute(
                    text(
                        "ALTER TABLE partner_policies ADD COLUMN IF NOT EXISTS "
                        "consent_method VARCHAR(20)"
                    )
                )
                # Partner-started OTP consent requests (NULL method = origination flow).
                for column, ddl in (
                    ("method", "VARCHAR(20)"),
                    ("created_by", "VARCHAR(255)"),
                    ("verification_note", "TEXT"),
                    ("verified_at", "TIMESTAMPTZ"),
                    ("otp_hash", "VARCHAR(128)"),
                    ("otp_expires_at", "TIMESTAMPTZ"),
                    ("otp_attempts", "INTEGER"),
                    ("otp_verified_at", "TIMESTAMPTZ"),
                    ("otp_channel", "VARCHAR(20)"),
                    ("otp_destination", "VARCHAR(255)"),
                    ("otp_provider", "VARCHAR(50)"),
                    ("otp_reference", "VARCHAR(255)"),
                    ("otp_send_count", "INTEGER"),
                    ("otp_last_sent_at", "TIMESTAMPTZ"),
                    ("otp_failures_total", "INTEGER"),
                ):
                    await conn.execute(
                        text(
                            f"ALTER TABLE consent_requests ADD COLUMN IF NOT EXISTS "
                            f"{column} {ddl}"
                        )
                    )
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_consent_requests_method "
                        "ON consent_requests (method)"
                    )
                )
            _logger.info("Database migration complete")

        asyncio.run(migrate())
