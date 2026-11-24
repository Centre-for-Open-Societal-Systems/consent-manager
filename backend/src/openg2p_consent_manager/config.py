from openg2p_fastapi_common.config import Settings as BaseSettings
from pydantic_settings import SettingsConfigDict

from . import __version__


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="consent_manager_", env_file=".env", extra="allow"
    )

    # Which API audience this instance serves — the platform's 4-API pattern
    # (staff / partner / beneficiary / agent). One image, one deployable per
    # audience; each mounts only its controllers and uses its own auth:
    #   staff       — Keycloak (staff realm): policy bindings, approvals, decisions
    #   partner     — PM keys (no Keycloak): the PDP /validate, status, receipts, JWKS
    #   beneficiary — Keycloak (beneficiary realm): /my/* + origination (deferred)
    #   all         — everything in one process (dev / back-compat default)
    api_audience: str = "all"

    openapi_title: str = "OpenG2P Consent Manager"
    openapi_description: str = """
        Consent Manager for OpenG2P.

        Acts as the Policy Decision Point (PDP) for outbound data sharing: it
        verifies partner-signed consent objects against each partner's onboarded
        policy and returns the effective set of fields a data holder (PEP) may
        release. Also issues canonical consent artefacts and signed receipts.
        """
    openapi_version: str = __version__

    # ── Database ────────────────────────────────────────────────────────────
    db_driver: str = "postgresql+asyncpg"
    db_username: str = "postgres"
    db_password: str = "postgres"
    db_hostname: str = "localhost"
    db_port: int = 5432
    db_dbname: str = "consent_manager_db"

    # Pooling is handled by openg2p-fastapi-common's async engine. For horizontal
    # scaling the app is fully stateless: scale by adding pods/workers and ensure
    # Postgres max_connections ≳ (pods × workers × pool_size + headroom).

    # ── Signing / trust ─────────────────────────────────────────────────────
    # CM receipt signing key. Preferred source is a PKCS#12 (.p12) keystore
    # holding the private key (+ certificate). Falls back to a PEM string, then
    # to a process-local ephemeral key (dev only). The signing algorithm is
    # auto-detected from the loaded key type (Ed25519→EdDSA, EC→ES256, RSA→RS256).
    cm_signing_p12_path: str = ""
    cm_signing_p12_password: str = ""
    cm_signing_private_key_pem: str = ""
    cm_signing_kid: str = "cm-2025-01"
    cm_signing_algorithm: str = "EdDSA"  # fallback hint only; key type wins
    # Set true by the Helm chart in demo mode so the service warns loudly that it
    # is signing with the public bundled demo key (must be replaced for production).
    cm_signing_is_demo: bool = False

    # NOTE: the data controller / module is a per-partner attribute
    # (Partner.controller_id), set at onboarding — one shared CM serves many
    # modules. A consent object's data_controller is validated against the
    # onboarded partner's controller_id, so there is no single global controller.

    # Replay window for embedded consent objects (seconds). issued_at must be
    # within now ± this skew.
    replay_freshness_window_sec: int = 300

    # ── OIDC (origination flow ID-token validation) ─────────────────────────
    # If oidc_jwks_url is set, ID tokens are signature-verified against the IdP
    # JWKS. Otherwise claims are read unverified (dev only) and token_validated
    # is recorded as false.
    oidc_jwks_url: str = ""
    oidc_issuer: str = ""
    oidc_audience: str = ""

    # ── Hot-path caching (pod-local, TTL) ───────────────────────────────────
    # The partner row + its active policy change rarely but are read on every
    # validate. Cached in-process per pod; staleness bounded by the TTL. (Keys
    # are NOT part of this — they live in Partner Management, see below.)
    partner_cache_ttl_sec: int = 60
    partner_cache_enabled: bool = True

    # ── Partner public keys (Partner Management service) ────────────────────
    # Partner signing keys are no longer stored in CM. They are owned by the
    # Partner Management (PM) service and fetched from its unauthenticated
    # key-fetch API: GET {partner_mgmt_api_url}/keys/{reference_id}. CM caches
    # them per pod with the discipline PM's Cache-Control implies. A partner's
    # PM reference is Partner.partner_mgmt_id (falling back to Partner.audience).
    #
    # Empty partner_mgmt_api_url disables PM fetching — verification then fails
    # closed (no keys → deny), which is the correct safe default until wired.
    partner_mgmt_api_url: str = ""  # e.g. http://commons-services-pm-partner-api
    # Crypto backend for verifying the partner's signed consent object (a compact
    # JWS). "partner-mgmt" verifies against keys fetched from PM via the shared
    # openg2p-fastapi-common CryptoHelper. "keymanager" (Mosip) and "local"
    # (seed keys, tests) remain selectable but are not the default.
    crypto_backend: str = "partner-mgmt"
    # Algorithms accepted on the partner consent JWS. The fastapi-common default
    # is "RS256" only; partners commonly use EdDSA/ES256, so widen it here.
    crypto_allowed_algorithms: str = "EdDSA,ES256,RS256"
    # Soft TTL: refresh window. Bounds how long a rotated/revoked key stays
    # trusted. Capped by the response's Cache-Control max-age when smaller.
    partner_key_cache_ttl_seconds: int = 300
    # Hard TTL: during a PM outage, serve last-known-good keys up to this age,
    # then fail closed.
    partner_key_hard_ttl_seconds: int = 21600
    # Negative cache: remember a 404 ("no keys") briefly to avoid hammering PM
    # for a disabled/unknown partner on every request.
    partner_key_negative_ttl_seconds: int = 30
    # Minimum interval between forced refetches for one partner (throttles the
    # unknown-kid refresh that catches key rotation immediately).
    partner_key_refresh_cooldown_seconds: int = 10
    # HTTP timeout (seconds) for a single key fetch from PM.
    partner_key_fetch_timeout_seconds: float = 3.0

    # ── Caller authentication (Keycloak / OIDC bearer) ──────────────────────
    # Validates bearer tokens on protected endpoints against the Keycloak JWKS,
    # exactly like the AWE service. When auth_enabled is false (dev), tokens are
    # accepted without verification and role checks pass.
    auth_enabled: bool = True
    auth_issuer: str = ""  # e.g. https://keycloak.../realms/staff
    auth_jwks_url: str = ""  # usually issuer + /protocol/openid-connect/certs
    auth_audience: str = ""  # optional; empty disables the audience check
    auth_algorithms: list[str] = ["RS256", "ES256", "EdDSA"]
    # Role (realm- or client-scoped) required for partner/policy admin endpoints.
    auth_admin_role: str = "CONSENT_MANAGER_ADMIN"
    # Default subject id-type when a token omits the subject_id_type claim.
    subject_default_id_type: str = "national_id"

    # Role required to act on AWE approval tasks via CM's proxy/inbox. Approvers
    # log into CM (not AWE) and CM proxies task-list/decision calls to AWE with
    # the approver's own JWT.
    auth_approver_role: str = "CONSENT_MANAGER_APPROVER"

    # ── Approval Workflow Engine (AWE) integration ──────────────────────────
    # Widening a partner's data-share POLICY is gated behind human approval in the
    # shared, per-environment AWE. CM is a *caller service*: on a widening it
    # submits an approval request and keeps the new policy version `pending`,
    # flipping it `active` only when AWE delivers a terminal `request_approved`
    # webhook. Approvers act in CM's OWN UI — CM proxies AWE's task-list/decision
    # endpoints with the approver's JWT (AWE has no approver UI, only /admin).
    #
    # When awe_enabled is false (default), a widening policy activates immediately
    # (no approval gate) — legacy behaviour.
    awe_enabled: bool = False
    # Base URL of the environment's AWE, reachable from CM pods (e.g.
    # https://awe.<baseDomain>). No trailing slash needed.
    awe_base_url: str = ""
    awe_http_timeout_seconds: float = 30.0
    # AWE approval policy (workflow: stages/approvers/SLA) that governs data-share
    # policy changes. Registered in AWE out-of-band. NB: distinct from CM's own
    # data-share policy — this is the *approval* policy key.
    awe_policy_change_policy_key: str = "consent-manager.policy_change.v1"
    # CM→AWE service auth: Keycloak client-credentials. The fetched bearer is
    # sent on POST /v1/awe/requests. If awe_static_token is set it is used
    # verbatim instead (dev/testing).
    awe_token_url: str = ""  # Keycloak token endpoint
    awe_client_id: str = ""
    awe_client_secret: str = ""
    awe_static_token: str = ""
    # Per-caller callback secret CM registered into the shared AWE DB. AWE looks
    # the raw secret up by this id; CM holds the same raw secret to verify the
    # HMAC on inbound webhooks. `callback_secret_id` is passed on every request.
    awe_callback_secret_id: str = ""
    awe_callback_hmac_secret: str = ""
    # Public URL AWE should POST terminal webhooks back to. Must resolve to
    # CM's /consent/v1/awe/webhooks/decision endpoint.
    awe_callback_url: str = ""
    # Reject webhooks whose signed timestamp is more than this far from now.
    awe_webhook_max_skew_sec: int = 300

    # ── Partner portal: partner users (consent scenario 1) — off by default ──
    # Partner users log in to the Keycloak `partner` realm (not the staff realm)
    # and carry a `partner_id` claim (the partner's audience, e.g. bank-a).
    # Empty issuer → the partner-portal API answers 404. Tokens are always
    # signature-verified against the realm's JWKS (empty JWKS URL → derived
    # from the issuer), independent of auth_enabled (which is for staff).
    partner_auth_issuer: str = ""  # e.g. https://keycloak.../realms/partner
    partner_auth_jwks_url: str = ""
    partner_auth_audience: str = ""  # optional; empty disables the audience check
    partner_auth_partner_claim: str = "partner_id"
    # A partner user needs at least one of these realm/client roles.
    partner_auth_roles: list[str] = ["PARTNER_OPERATOR", "PARTNER_ADMIN"]

    # ── OTP consent: partner raises a request, the CM sends the subject a code,
    # the partner submits it and the consent is granted (partner portal API) ──
    otp_provider: str = "fayda"  # fayda | internal (delivered by SMS when oan_delivery_enabled)
    otp_length: int = 6
    otp_ttl_sec: int = 300
    otp_max_attempts: int = 3  # wrong codes per issued code
    # Budgets that a resend cannot reset:
    otp_resend_cooldown_sec: int = 60  # between two codes for one request
    otp_max_sends_per_request: int = 3  # the first code + 2 resends
    otp_max_failures_per_request: int = 6  # wrong codes over all its codes; then cancelled
    # Codes sent to one subject (any partner) within the window.
    otp_subject_window_sec: int = 3600
    otp_max_sends_per_subject: int = 10
    # Mixed into the stored hash so a leaked row cannot be brute-forced offline.
    # REQUIRED: no default, so no environment shares a public value. Without it
    # no code is issued (503).
    otp_salt: str = ""
    otp_channel: str = "PHONE"
    # TEST HARNESS: write each live code to the service log. Off by default;
    # never on where real subjects exist. Without this or an SMS transport there
    # is no way to deliver a code, so none is issued (503).
    otp_log_codes: bool = False

    # ── OTP delivery by SMS through the OAN Messaging Service ───────────────
    # Off by default. When on (with a base URL and token), every issued code is
    # sent as one SMS (purpose "otp", expiring with the code itself), and an
    # issue that cannot be delivered fails visibly instead of storing the code.
    oan_delivery_enabled: bool = False
    oan_base_url: str = ""  # e.g. https://oan-messaging.<env>  (no path)
    oan_token: str = ""  # caller credential from the OAN operator; secret
    oan_ca_bundle: str = ""  # optional CA bundle path for the OAN API
    oan_timeout_seconds: float = 5.0
    oan_max_attempts: int = 2  # client retries, same key and body
    # Subject -> phone resolution. "map" reads oan_otp_phone_map (dev/staging test
    # numbers only). A production resolver (the registered phone from the
    # beneficiary API, approved by the identity owner) replaces it; see
    # services/otp_delivery.py.
    oan_phone_resolver: str = "map"
    # JSON: {"<subject_id_value>": "0900000101"}
    oan_otp_phone_map: str = ""

    @property
    def oan_otp_phones(self) -> dict:
        import json

        raw = (self.oan_otp_phone_map or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:  # never log the value: it holds phones
            return {}
        return parsed if isinstance(parsed, dict) else {}

    # ── OTP delivery by SMS straight to the 8028 gateway ────────────────────
    # An alternative to the OAN Messaging Service for the OTP only (services/sms8028.py).
    # Off by default. Never switch it on together with oan_delivery_enabled: the code
    # would be sent twice, so that combination is refused (503).
    sms8028_enabled: bool = False
    sms8028_base_url: str = "https://8028.et:3400/sms-api/v1"
    sms8028_client_secret: str = ""  # secret, from 8028
    # The client certificate and key 8028 registered for us, as PEM text (or base64 of it);
    # secrets. They are loaded from memory-only settings, never from the image.
    sms8028_client_cert_pem: str = ""
    sms8028_client_key_pem: str = ""
    sms8028_timeout_seconds: float = 10.0
    sms8028_max_attempts: int = 2  # only for "nothing reached 8028"; a written request is never repeated

    @property
    def oan_delivery_ready(self) -> bool:
        """SMS delivery is switched on and has somewhere to send."""
        return bool(self.oan_delivery_enabled and self.oan_base_url and self.oan_token)

    @property
    def sms8028_ready(self) -> bool:
        """Direct 8028 delivery is switched on and has its secret, certificate and key."""
        return bool(
            self.sms8028_enabled
            and self.sms8028_client_secret
            and self.sms8028_client_cert_pem
            and self.sms8028_client_key_pem
        )

    @property
    def sms_delivery_ready(self) -> bool:
        """Some SMS delivery (OAN or 8028) is on: an issued code is sent, not only logged."""
        return bool(self.oan_delivery_ready or self.sms8028_ready)
