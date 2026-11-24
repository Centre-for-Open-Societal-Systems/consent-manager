"""Partner-started OTP consent: ``POST /consent-requests`` (method ``otp``) sends the
subject a code at once; the partner's ``verify-otp`` with that code grants the
consent, which is then presented at ``/validate`` by its ID. Every call is scoped
to the calling partner. Delivery is mocked: no SMS and no live OTP.
"""
from datetime import datetime, timedelta, timezone

import pytest

PORTAL_REQUESTS = "/consent-requests"
SUBJECT = "123456789012"


@pytest.fixture(autouse=True)
def otp_config(monkeypatch):
    """A configured salt and the log as the (test) delivery channel. The
    per-subject budget is lifted: every test here uses the same subject."""
    from openg2p_consent_manager.config import Settings

    config = Settings.get_config()
    monkeypatch.setattr(config, "otp_salt", "test-only-salt")
    monkeypatch.setattr(config, "otp_log_codes", True)
    monkeypatch.setattr(config, "otp_max_sends_per_subject", 10_000)
    return config


@pytest.fixture
def codes(monkeypatch):
    """The codes the CM generated, newest last (nothing is sent anywhere)."""
    from openg2p_consent_manager.utils import fayda_otp

    sent = []
    real = fayda_otp.generate_otp

    def capture(length=6):
        code = real(length)
        sent.append(code)
        return code

    monkeypatch.setattr(fayda_otp, "generate_otp", capture)
    return sent


def backdate_last_send(run, request_id, seconds=3600):
    """As if the last code had been sent ``seconds`` ago (past the cooldown)."""
    from openg2p_consent_manager.db import async_session
    from openg2p_consent_manager.models import ConsentRequest

    async def go():
        async with async_session()() as session:
            row = await session.get(ConsentRequest, request_id)
            row.otp_last_sent_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
            await session.commit()

    run(go())


def otp_body(**extra):
    body = {
        "method": "otp",
        "subject_id": {"type": "FAYDA_FAN", "value": SUBJECT},
        "purpose": {"code": "credit-assessment"},
        "data_scopes": ["farmer_personal_details"],
    }
    body.update(extra)
    return body


def raise_request(pc, **extra):
    status, body = pc("POST", PORTAL_REQUESTS, json=otp_body(**extra))
    assert status == 201, body
    return body


def wrong(code):
    return "000000" if code != "000000" else "111111"


def validate_by_id(call, consent_id, audience):
    return call("POST", "/consent/v1/validate", json={
        "consent_id": consent_id, "consent_partner_id": audience,
        "request_context": {"subject_id": {"type": "FAYDA_FAN", "value": SUBJECT}},
    })


def test_raise_sends_the_code(as_partner, bank, codes):
    pc = as_partner(bank.audience, username="api-client")
    req = raise_request(pc)
    assert req["status"] == "pending" and req["method"] == "otp"
    assert req["created_by"] == "api-client" and req["consent_id"] is None
    assert req["otp_channel"] == "PHONE" and req["otp_expires_at"]
    assert req["otp_attempts_left"] == 3
    assert len(codes) == 1 and len(codes[0]) == 6
    assert "otp" not in req  # the code itself is never returned

    status, again = pc("GET", f"{PORTAL_REQUESTS}/{req['id']}")
    assert status == 200 and again["id"] == req["id"]


def test_only_otp_requests_are_accepted(as_partner, bank, codes):
    pc = as_partner(bank.audience)
    status, body = pc("POST", PORTAL_REQUESTS, json=otp_body(method="assisted"))
    assert status == 400  # the framework's validation error: only "otp" exists in this release
    assert codes == []


def test_requests_need_a_partner_token(call, bank):
    status, _ = call("POST", "/consent/v1/partner-portal/consent-requests", json=otp_body())
    assert status == 401


def test_raise_uses_the_policy_checks(as_partner, bank, codes):
    pc = as_partner(bank.audience)
    status, body = pc("POST", PORTAL_REQUESTS, json=otp_body(purpose={"code": "marketing"}))
    assert status == 422 and "purpose_not_allowed" in body["error"]
    status, body = pc("POST", PORTAL_REQUESTS, json=otp_body(
        subject_id={"type": "PASSPORT", "value": SUBJECT}))
    assert status == 422 and "subject_not_allowed" in body["error"]
    status, body = pc("POST", PORTAL_REQUESTS, json=otp_body(data_scopes=["bank_balance"]))
    assert status == 422 and "scope_exceeds_policy" in body["error"]
    assert codes == []  # nothing sent for a refused request


def test_unknown_partner_cannot_raise(as_partner, codes):
    status, body = as_partner("no-such-partner")("POST", PORTAL_REQUESTS, json=otp_body())
    assert status in (403, 404, 422)
    assert codes == []


def test_verify_grants_the_consent(call, as_partner, bank, codes):
    pc = as_partner(bank.audience, username="api-client")
    req = raise_request(pc)
    status, consent = pc("POST", f"{PORTAL_REQUESTS}/{req['id']}/verify-otp",
                         json={"otp": codes[-1]})
    assert status == 201, consent
    assert consent["status"] == "active" and consent["consent_id"] and consent["receipt_id"]
    assert consent["consent_request_id"] == req["id"]
    assert consent["effective_data_scopes"] == ["farmer_personal_details"]
    assert consent["auth_method"] == "otp"

    status, req_now = pc("GET", f"{PORTAL_REQUESTS}/{req['id']}")
    assert req_now["status"] == "approved" and req_now["consent_id"] == consent["consent_id"]

    # The receipt is signed and fetchable.
    status, receipt = call("GET", f"/consent/v1/receipts/{consent['receipt_id']}")
    assert status == 200 and receipt["consent_artefact"]["@id"] == consent["consent_id"]

    # And the consent is presented by its ID at /validate.
    status, decision = validate_by_id(call, consent["consent_id"], bank.audience)
    assert status == 200 and decision["decision"] == "permit", decision
    assert decision["effective_data_scopes"] == ["farmer_personal_details"]

    # The code is spent.
    status, body = pc("POST", f"{PORTAL_REQUESTS}/{req['id']}/verify-otp",
                      json={"otp": codes[-1]})
    assert status == 409 and "approved" in body["error"]


def test_wrong_code_counts_attempts(as_partner, bank, codes):
    pc = as_partner(bank.audience)
    req = raise_request(pc)
    path = f"{PORTAL_REQUESTS}/{req['id']}/verify-otp"
    status, body = pc("POST", path, json={"otp": wrong(codes[-1])})
    assert status == 401 and body["error"] == "otp_invalid"
    assert pc("GET", f"{PORTAL_REQUESTS}/{req['id']}")[1]["otp_attempts_left"] == 2
    status, consent = pc("POST", path, json={"otp": codes[-1]})
    assert status == 201, consent


def test_attempt_limit_blocks_even_the_right_code(as_partner, bank, codes):
    pc = as_partner(bank.audience)
    req = raise_request(pc)
    path = f"{PORTAL_REQUESTS}/{req['id']}/verify-otp"
    for _ in range(3):
        assert pc("POST", path, json={"otp": wrong(codes[-1])})[0] == 401
    status, body = pc("POST", path, json={"otp": codes[-1]})
    assert status == 429 and body["error"] == "too_many_attempts"


def test_resend_cannot_undo_the_failure_limit(as_partner, bank, codes, run):
    """Six wrong codes over all codes cancel the request; a resend resets the
    per-code attempts only."""
    pc = as_partner(bank.audience)
    req = raise_request(pc)
    path = f"{PORTAL_REQUESTS}/{req['id']}/verify-otp"
    for _ in range(3):
        pc("POST", path, json={"otp": wrong(codes[-1])})
    backdate_last_send(run, req["id"])
    assert pc("POST", f"{PORTAL_REQUESTS}/{req['id']}/resend-otp")[0] == 200
    for _ in range(2):
        assert pc("POST", path, json={"otp": wrong(codes[-1])})[0] == 401
    status, body = pc("POST", path, json={"otp": wrong(codes[-1])})
    assert status == 429 and body["error"] == "otp_failure_limit_reached"
    status, body = pc("GET", f"{PORTAL_REQUESTS}/{req['id']}")
    assert body["status"] == "cancelled"
    status, body = pc("POST", path, json={"otp": codes[-1]})
    assert status == 409 and "cancelled" in body["error"]


def test_resend_issues_a_new_code_and_the_old_one_stops(as_partner, bank, codes, run):
    pc = as_partner(bank.audience)
    req = raise_request(pc)
    first = codes[-1]
    status, body = pc("POST", f"{PORTAL_REQUESTS}/{req['id']}/resend-otp")
    assert status == 429 and body["error"] == "resend_too_soon"
    backdate_last_send(run, req["id"])
    status, body = pc("POST", f"{PORTAL_REQUESTS}/{req['id']}/resend-otp")
    assert status == 200 and len(codes) == 2
    if codes[1] != first:
        status, _ = pc("POST", f"{PORTAL_REQUESTS}/{req['id']}/verify-otp", json={"otp": first})
        assert status == 401
    status, consent = pc("POST", f"{PORTAL_REQUESTS}/{req['id']}/verify-otp",
                         json={"otp": codes[-1]})
    assert status == 201, consent


def test_send_limit_per_request(as_partner, bank, codes, run):
    pc = as_partner(bank.audience)
    req = raise_request(pc)
    for _ in range(2):
        backdate_last_send(run, req["id"])
        assert pc("POST", f"{PORTAL_REQUESTS}/{req['id']}/resend-otp")[0] == 200
    backdate_last_send(run, req["id"])
    status, body = pc("POST", f"{PORTAL_REQUESTS}/{req['id']}/resend-otp")
    assert status == 429 and body["error"] == "resend_limit_reached"
    assert len(codes) == 3


def test_send_limit_per_subject(as_partner, bank, codes, otp_config, monkeypatch):
    monkeypatch.setattr(otp_config, "otp_max_sends_per_subject", 2)
    pc = as_partner(bank.audience)
    subject = {"type": "FAYDA_FAN", "value": f"budget-{bank.audience}"}  # a subject of its own
    raise_request(pc, subject_id=subject)
    raise_request(pc, subject_id=subject)
    status, body = pc("POST", PORTAL_REQUESTS, json=otp_body(subject_id=subject))
    assert status == 429 and body["error"] == "subject_otp_limit_reached"
    assert len(codes) == 2
    # Another subject is not affected.
    raise_request(pc, subject_id={"type": "FAYDA_FAN", "value": f"other-{bank.audience}"})


def test_expired_code(as_partner, bank, codes, run):
    from openg2p_consent_manager.db import async_session
    from openg2p_consent_manager.models import ConsentRequest

    pc = as_partner(bank.audience)
    req = raise_request(pc)

    async def expire():
        async with async_session()() as session:
            row = await session.get(ConsentRequest, req["id"])
            row.otp_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()

    run(expire())
    status, body = pc("POST", f"{PORTAL_REQUESTS}/{req['id']}/verify-otp",
                      json={"otp": codes[-1]})
    assert status == 410 and body["error"] == "otp_expired"


def test_no_salt_no_code(as_partner, bank, codes, otp_config, monkeypatch):
    monkeypatch.setattr(otp_config, "otp_salt", "")
    status, body = as_partner(bank.audience)("POST", PORTAL_REQUESTS, json=otp_body())
    assert status == 503 and body["error"] == "otp_unavailable"
    assert codes == []


def test_no_delivery_channel_no_code(as_partner, bank, codes, otp_config, monkeypatch):
    monkeypatch.setattr(otp_config, "otp_log_codes", False)
    status, body = as_partner(bank.audience)("POST", PORTAL_REQUESTS, json=otp_body())
    assert status == 503 and body["error"] == "otp_unavailable"
    assert codes == []


def test_another_partners_request_is_not_found(as_partner, new_bank, bank, codes):
    other = new_bank()
    mine, theirs = as_partner(bank.audience), as_partner(other.audience)
    req = raise_request(mine)
    path = f"{PORTAL_REQUESTS}/{req['id']}"
    assert theirs("GET", path)[0] == 404
    status, _ = theirs("POST", f"{path}/verify-otp", json={"otp": codes[-1]})
    assert status == 404
    assert theirs("POST", f"{path}/resend-otp")[0] == 404
    # Nothing was spent by the other partner's attempts.
    status, consent = mine("POST", f"{path}/verify-otp", json={"otp": codes[-1]})
    assert status == 201, consent


def test_another_partner_cannot_present_the_consent(call, as_partner, new_bank, bank, codes):
    other = new_bank()
    pc = as_partner(bank.audience)
    req = raise_request(pc)
    status, consent = pc("POST", f"{PORTAL_REQUESTS}/{req['id']}/verify-otp",
                         json={"otp": codes[-1]})
    assert status == 201
    status, decision = validate_by_id(call, consent["consent_id"], other.audience)
    assert status == 200 and decision["decision"] == "deny"
    # No consent_partner_id: refused, not defaulted.
    status, decision = call("POST", "/consent/v1/validate",
                            json={"consent_id": consent["consent_id"]})
    assert decision["decision"] == "deny"


def test_verify_applies_the_policy_as_it_is_now(as_partner, bank, codes):
    pc = as_partner(bank.audience)
    req = raise_request(pc)
    bank.set_policy(purposes=("insurance",))  # narrows: the requested purpose is gone
    status, body = pc("POST", f"{PORTAL_REQUESTS}/{req['id']}/verify-otp",
                      json={"otp": codes[-1]})
    assert status == 422 and "purpose_not_allowed" in body["error"]
    # The code is spent either way; no consent exists.
    status, body = pc("POST", f"{PORTAL_REQUESTS}/{req['id']}/verify-otp",
                      json={"otp": codes[-1]})
    assert status == 409 and body["error"] == "already_verified"
    assert pc("GET", f"{PORTAL_REQUESTS}/{req['id']}")[1]["consent_id"] is None


def test_verify_narrows_scopes_to_the_current_policy(as_partner, bank, codes):
    pc = as_partner(bank.audience)
    req = raise_request(pc, data_scopes=["farmer_personal_details", "farm_details"])
    bank.set_policy(scopes=("farm_details",))
    status, consent = pc("POST", f"{PORTAL_REQUESTS}/{req['id']}/verify-otp",
                         json={"otp": codes[-1]})
    assert status == 201, consent
    assert consent["effective_data_scopes"] == ["farm_details"]


def test_validate_by_id_applies_the_policy_as_it_is_now(call, as_partner, bank, codes):
    pc = as_partner(bank.audience)
    req = raise_request(pc)
    status, consent = pc("POST", f"{PORTAL_REQUESTS}/{req['id']}/verify-otp",
                         json={"otp": codes[-1]})
    assert status == 201
    bank.set_policy(purposes=("insurance",))
    status, decision = validate_by_id(call, consent["consent_id"], bank.audience)
    assert decision["decision"] == "deny" and decision["reason_code"] == "purpose_not_allowed"


def test_the_subject_flow_cannot_approve_an_otp_request(as_partner, bank, codes, run):
    """An OTP request is closed to the origination flow: it is granted only by its code."""
    from openg2p_consent_manager.services import LifecycleError, LifecycleService

    pc = as_partner(bank.audience)
    req = raise_request(pc)
    lifecycle = LifecycleService.get_component()
    assert run(lifecycle.get_request(req["id"])) is None
    with pytest.raises(LifecycleError) as exc:
        run(lifecycle.approve(req["id"], ["farmer_personal_details"]))
    assert exc.value.status_code == 404
    with pytest.raises(LifecycleError):
        run(lifecycle.deny(req["id"], "no"))
    assert pc("GET", f"{PORTAL_REQUESTS}/{req['id']}")[1]["status"] == "pending"
