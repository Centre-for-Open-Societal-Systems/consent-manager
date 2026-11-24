"""Backend integration tests for the Consent Manager.

They run the real services, controllers and ORM models against a database. By
default that is a throwaway SQLite file (the framework supports it; the JSONB
columns are compiled as JSON for it below), so no server is needed. To run
against Postgres instead:

    CM_TEST_DB_DATASOURCE=postgresql+asyncpg://postgres@localhost:5432/cm_test \
        pytest backend/tests

Tables are created from the models; the Postgres-only ``ALTER TABLE`` migration in
``Initializer.migrate_database`` is not exercised here. Partner signing keys
normally come from Partner Management; here the verifier is replaced by an
in-process one holding test keys. Partner-realm authentication is replaced by a
dependency override that reads the partner from the ``X-Test-Partner`` header.
"""
import asyncio
import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest

_DEFAULT_DB = "sqlite+aiosqlite:///" + os.path.join(
    tempfile.gettempdir(), f"cm_test_{uuid.uuid4().hex[:8]}.db"
).replace("\\", "/")
DATASOURCE = os.environ.get("CM_TEST_DB_DATASOURCE", _DEFAULT_DB)

# Settings are read once at import — set them before importing the app.
os.environ["CONSENT_MANAGER_DB_DATASOURCE"] = DATASOURCE
os.environ["CONSENT_MANAGER_AUTH_ENABLED"] = "false"
os.environ["CONSENT_MANAGER_AWE_ENABLED"] = "false"
os.environ["CONSENT_MANAGER_API_AUDIENCE"] = "all"
os.environ["CONSENT_MANAGER_PARTNER_CACHE_ENABLED"] = "false"
os.environ.setdefault("CONSENT_MANAGER_LOGGING_LEVEL", "WARNING")

# JSONB is Postgres-only; render it as JSON on SQLite.
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):
    return "JSON"


@pytest.fixture(scope="session")
def loop():
    lp = asyncio.new_event_loop()
    yield lp
    lp.close()


class FakePartnerKeys:
    """Stands in for the Partner-Management-backed crypto helper: verifies a
    compact JWS against a test public key registered per PM reference id."""

    def __init__(self):
        self.keys = {}

    def register(self, km_ref_id, public_key):
        self.keys[km_ref_id] = public_key

    async def verify_jwt(self, jws, km_ref_id=None, **_):
        from jwt.api_jws import PyJWS

        key = self.keys.get(km_ref_id)
        if key is None:
            return False
        try:
            PyJWS().decode(jws, key, algorithms=["EdDSA"])
            return True
        except Exception:
            return False


@pytest.fixture(scope="session")
def env(loop):
    """Initialise the app on an empty database with every table created."""
    from openg2p_fastapi_common.context import app_registry

    from openg2p_consent_manager import models
    from openg2p_consent_manager.app import Initializer
    from openg2p_consent_manager.auth import PartnerUser, current_partner_user
    from openg2p_consent_manager.services import VerificationService

    init = Initializer()

    async def create_tables():
        for name in (
            "Partner", "PartnerPolicy", "ConsentRequest", "AuthContext", "ConsentArtefact",
            "ConsentReceipt", "RevocationRecord", "DecisionLog", "AuditLog",
            "AweProcessedEvent",
        ):
            await getattr(models, name).create_migrate()

    loop.run_until_complete(create_tables())

    app = app_registry.get()
    from fastapi import Header

    async def test_partner(
        x_test_partner: str = Header(default=None), x_test_user: str = Header(default="tester"),
    ):
        from fastapi import HTTPException

        if not x_test_partner:
            raise HTTPException(401, "Missing bearer token")
        return PartnerUser(username=x_test_user, name=None, partner_id=x_test_partner,
                           roles=["PARTNER_OPERATOR"])

    app.dependency_overrides[current_partner_user] = test_partner

    keys = FakePartnerKeys()
    VerificationService.get_component().crypto_helper = keys
    return {"app": app, "keys": keys, "loop": loop, "init": init}


@pytest.fixture(scope="session")
def call(env):
    """``call(method, path, json=..., headers=...)`` → (status, body) against the ASGI app."""
    import httpx

    transport = httpx.ASGITransport(app=env["app"])

    def _call(method, path, json=None, params=None, headers=None, content=None):
        async def go():
            async with httpx.AsyncClient(transport=transport, base_url="http://cm") as c:
                r = await c.request(
                    method, path, json=json, params=params, headers=headers, content=content
                )
                try:
                    body = r.json()
                except ValueError:
                    body = r.text
                return r.status_code, body

        return env["loop"].run_until_complete(go())

    return _call


@pytest.fixture(scope="session")
def run(env):
    return env["loop"].run_until_complete


class Bank:
    """A test partner binding: a PM key, a binding row and an active policy."""

    def __init__(self, env, run, *, consent_method=None, controller="farmer-registry",
                 purposes=("credit-assessment",), subject_types=("FAYDA_FAN",),
                 scopes=("farmer_personal_details", "farm_details"), max_validity=None):
        from cryptography.hazmat.primitives.asymmetric import ed25519

        from openg2p_consent_manager.services import PartnerService

        self.audience = f"bank-{uuid.uuid4().hex[:8]}"
        self.pm_id = f"PM_{self.audience}"
        self.controller = controller
        self.key = ed25519.Ed25519PrivateKey.generate()
        env["keys"].register(self.pm_id, self.key.public_key())
        self.partners = PartnerService.get_component()
        self._run = run

        async def bind():
            partner = await self.partners.create_partner(
                type("D", (), {"audience": self.audience, "controller_id": controller,
                               "name": "Test Bank", "partner_mgmt_id": self.pm_id})()
            )
            self.id = partner.id
            return partner

        run(bind())
        self.set_policy(consent_method=consent_method, purposes=purposes,
                        subject_types=subject_types, scopes=scopes, max_validity=max_validity)

    def set_policy(self, *, consent_method=None, purposes=("credit-assessment",),
                   subject_types=("FAYDA_FAN",),
                   scopes=("farmer_personal_details", "farm_details"), max_validity=None):
        from openg2p_consent_manager.schemas.partner import PolicyUpsert

        fields = dict(
            allowed_data_scopes=list(scopes), allowed_purposes=list(purposes),
            allowed_subject_id_types=list(subject_types), allowed_signing_algs=["EdDSA"],
            max_validity_duration=max_validity, fetch_type="oneshot",
        )
        if consent_method is not None:
            fields["consent_method"] = consent_method
        self._run(self.partners.upsert_policy(self.id, PolicyUpsert(**fields)))

    def sign(self, claims: dict) -> str:
        from jwt.api_jws import PyJWS

        payload = json.dumps(claims, sort_keys=True, separators=(",", ":"), default=str)
        return PyJWS().encode(payload.encode(), self.key, algorithm="EdDSA",
                              headers={"kid": "k1"})

    def claims(self, subject_value="123456789012", subject_type="FAYDA_FAN", **extra):
        now = datetime.now(timezone.utc)
        base = {
            "jti": str(uuid.uuid4()),
            "aud": self.audience,
            "data_controller": self.controller,
            "subject_id": {"type": subject_type, "value": subject_value},
            "purpose": {"code": "credit-assessment"},
            "data_scopes": ["farmer_personal_details"],
            "fetch_type": "oneshot",
            "validity": {
                "valid_from": (now - timedelta(minutes=1)).isoformat(),
                "valid_until": (now + timedelta(days=30)).isoformat(),
            },
            "issued_at": now.isoformat(),
        }
        base.update(extra)
        return base


@pytest.fixture
def new_bank(env, run):
    return lambda **kw: Bank(env, run, **kw)


@pytest.fixture
def bank(new_bank):
    return new_bank()


@pytest.fixture
def as_partner(call):
    """``as_partner(audience, username=...)`` → ``pc(method, path, json=...)`` acting as
    a partner-realm user of that partner (the token check is replaced; see module doc)."""

    def make(audience, username="tester"):
        headers = {"X-Test-Partner": audience, "X-Test-User": username}
        return lambda method, path, json=None, **kw: call(
            method, "/consent/v1/partner-portal" + path, json=json, headers=headers, **kw
        )

    return make
