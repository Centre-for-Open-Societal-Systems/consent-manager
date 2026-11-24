"""``consent_method`` on the partner policy, and the checks that keep the OTP path from
being bypassed. NULL (every policy before the field) behaves exactly as before."""
from datetime import datetime, timedelta, timezone

import pytest
from test_otp_consent import (  # noqa: F401 - fixtures
    PORTAL_REQUESTS,
    codes,
    otp_config,
    raise_request,
    validate_by_id,
)


def validate_jws(call, bank, jws):
    return call("POST", "/consent/v1/validate", json={"consent_jws": jws})


def active_policy(run, bank):
    return run(bank.partners.get_policy(bank.id))


# ── the signed (non-OTP) path ────────────────────────────────────────────────


def test_a_policy_without_a_method_still_permits_a_signed_consent(call, bank):
    """The 1.0 behaviour, unchanged."""
    status, decision = validate_jws(call, bank, bank.sign(bank.claims()))
    assert status == 200 and decision["decision"] == "permit", decision


def test_partner_signed_and_internal_permit_a_signed_consent(call, new_bank):
    for method in ("partner_signed", "internal"):
        bank = new_bank(consent_method=method)
        status, decision = validate_jws(call, bank, bank.sign(bank.claims()))
        assert decision["decision"] == "permit", (method, decision)


def test_an_otp_policy_refuses_a_signed_consent(call, new_bank):
    bank = new_bank(consent_method="otp")
    status, decision = validate_jws(call, bank, bank.sign(bank.claims()))
    assert status == 200 and decision["decision"] == "deny"
    assert decision["reason_code"] == "consent_method_not_allowed"


def test_a_jti_permitted_earlier_is_refused_once_the_policy_requires_otp(call, bank):
    jws = bank.sign(bank.claims())
    assert validate_jws(call, bank, jws)[1]["decision"] == "permit"
    assert validate_jws(call, bank, jws)[1]["decision"] == "permit"  # idempotent
    bank.set_policy(consent_method="otp")
    status, decision = validate_jws(call, bank, jws)
    assert decision["decision"] == "deny"
    assert decision["reason_code"] == "consent_method_not_allowed"


def test_an_otp_policy_accepts_the_otp_consent_by_id(call, as_partner, new_bank, codes):  # noqa: F811
    bank = new_bank(consent_method="otp")
    pc = as_partner(bank.audience)
    req = raise_request(pc)
    status, consent = pc("POST", f"{PORTAL_REQUESTS}/{req['id']}/verify-otp",
                         json={"otp": codes[-1]})
    assert status == 201, consent
    status, decision = validate_by_id(call, consent["consent_id"], bank.audience)
    assert decision["decision"] == "permit", decision


def test_a_lifecycle_consent_is_not_presentable_by_id(call, run, bank):
    """Only a consent an OTP confirmed can be presented by consent_id; an originated
    consent from the subject's own login keeps its 1.0 behaviour (not presentable)."""
    from openg2p_consent_manager.models import ArtefactSource, ConsentArtefact
    from openg2p_consent_manager.db import async_session

    now = datetime.now(timezone.utc)

    async def make():
        async with async_session()() as session:
            row = ConsentArtefact(
                subject_id_type="FAYDA_FAN", subject_id_value="123456789012",
                controller_id=bank.controller, partner_id=bank.id,
                purpose={"code": "credit-assessment"}, data_scopes=["farm_details"],
                effective_data_scopes=["farm_details"], valid_from=now,
                valid_until=now + timedelta(days=1), source=ArtefactSource.originated.value,
                status="active",
            )
            session.add(row)
            await session.commit()
            return row.id

    consent_id = run(make())
    status, decision = validate_by_id(call, consent_id, bank.audience)
    assert decision["decision"] == "deny" and decision["reason_code"] == "unknown_consent"


def test_validate_needs_exactly_one_consent(call, bank):
    status, decision = call("POST", "/consent/v1/validate", json={})
    assert decision["decision"] == "deny" and decision["reason_code"] == "malformed_object"
    status, decision = call("POST", "/consent/v1/validate", json={
        "consent_jws": "a.b.c", "consent_id": "x"})
    assert decision["decision"] == "deny" and decision["reason_code"] == "malformed_object"


# ── the field on the policy ──────────────────────────────────────────────────


def policy_upsert(**fields):
    from openg2p_consent_manager.schemas.partner import PolicyUpsert

    base = dict(allowed_data_scopes=["farm_details"], allowed_purposes=["credit-assessment"],
                allowed_subject_id_types=["FAYDA_FAN"], allowed_signing_algs=["EdDSA"])
    base.update(fields)
    return PolicyUpsert(**base)


def test_a_new_policy_has_no_method(run, bank):
    assert active_policy(run, bank).consent_method is None


def test_a_policy_put_without_the_field_keeps_the_method(run, new_bank):
    bank = new_bank(consent_method="otp")
    assert active_policy(run, bank).consent_method == "otp"
    run(bank.partners.upsert_policy(bank.id, policy_upsert()))
    assert active_policy(run, bank).consent_method == "otp"


def test_null_resets_the_method(run, new_bank):
    bank = new_bank(consent_method="otp")
    run(bank.partners.upsert_policy(bank.id, policy_upsert(consent_method=None)))
    assert active_policy(run, bank).consent_method is None


def test_an_unknown_method_is_refused():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        policy_upsert(consent_method="telepathy")
    assert policy_upsert(consent_method=" OTP ").consent_method == "otp"


def test_a_less_strict_method_is_a_widening(run, new_bank):
    from openg2p_consent_manager.services import PartnerService

    bank = new_bank(consent_method="otp")
    active = active_policy(run, bank)
    widen = PartnerService._is_widening
    assert widen(policy_upsert(consent_method="partner_signed"), active) is True
    assert widen(policy_upsert(consent_method="internal"), active) is True
    assert widen(policy_upsert(consent_method="otp"), active) is False

    signed = new_bank(consent_method="partner_signed")
    assert widen(policy_upsert(consent_method="otp"), active_policy(run, signed)) is False
    assert widen(policy_upsert(consent_method=None), active_policy(run, signed)) is False
    assert widen(policy_upsert(consent_method="internal"), active_policy(run, signed)) is True


# ── the 1.0 origination flow is unchanged ────────────────────────────────────


def test_the_origination_flow_still_grants_a_consent(run, bank):
    import jwt

    from openg2p_consent_manager.schemas.lifecycle import ConsentRequestCreate
    from openg2p_consent_manager.services import LifecycleService

    lifecycle = LifecycleService.get_component()
    subject = "origination-subject"
    data = ConsentRequestCreate(
        subject_id={"type": "FAYDA_FAN", "value": subject}, partner_id=bank.id,
        purpose={"code": "credit-assessment"}, requested_scopes=["farm_details"],
    )
    req = run(lifecycle.create_request(data))
    assert req.status == "pending" and req.method is None
    id_token = jwt.encode({"sub": subject, "iss": "test", "amr": ["otp"]}, "k", algorithm="HS256")
    run(lifecycle.authenticate(req.id, id_token))
    artefact = run(lifecycle.approve(req.id, ["farm_details"]))
    assert artefact.status == "active" and artefact.effective_data_scopes == ["farm_details"]
