"""Postgres-only integration coverage: the upgrade of an untouched 1.0 schema, and two
simultaneous verifications of the same code. Synthetic data only, no SMS.

Skipped unless CM_TEST_DB_DATASOURCE points at Postgres. Because the upgrade test DROPS
SCHEMA public, it also refuses to run unless ALL of these hold: the host is localhost or
127.0.0.1, the database name contains "test", and CM_TEST_ALLOW_SCHEMA_RESET=1.

    CM_TEST_DB_DATASOURCE=postgresql+asyncpg://postgres@127.0.0.1:5432/cm_test \
    CM_TEST_ALLOW_SCHEMA_RESET=1 \
        python -m pytest backend/tests/test_postgres_integration.py -v
"""
import asyncio
import os
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import urlparse

import pytest
from test_otp_consent import (  # noqa: F401 - fixtures
    PORTAL_REQUESTS,
    SUBJECT,
    codes,
    otp_config,
    otp_body,
)

import conftest

_url = urlparse(conftest.DATASOURCE.replace("+asyncpg", ""))
_is_pg = conftest.DATASOURCE.startswith("postgresql")
_safe = (
    _is_pg
    and _url.hostname in ("localhost", "127.0.0.1")
    and "test" in (_url.path or "").lower()
    and os.environ.get("CM_TEST_ALLOW_SCHEMA_RESET") == "1"
)

pytestmark = pytest.mark.skipif(
    not _safe,
    reason="needs an isolated localhost Postgres whose name contains 'test' and "
    "CM_TEST_ALLOW_SCHEMA_RESET=1",
)

# The tables this change alters, exactly as release 1.0 created them.
LEGACY_DDL = [
    """CREATE TABLE partners (
        id VARCHAR PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ,
        name VARCHAR(255), audience VARCHAR(255) NOT NULL, controller_id VARCHAR(255) NOT NULL,
        status VARCHAR(20) NOT NULL, partner_mgmt_id VARCHAR(255))""",
    "CREATE UNIQUE INDEX ix_partners_audience ON partners (audience)",
    """CREATE TABLE partner_policies (
        id VARCHAR PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ,
        partner_id VARCHAR NOT NULL, version INTEGER NOT NULL, status VARCHAR(20) NOT NULL,
        allowed_data_scopes JSONB NOT NULL, allowed_purposes JSONB NOT NULL,
        allowed_subject_id_types JSONB NOT NULL, allowed_signing_algs JSONB NOT NULL,
        max_validity_duration VARCHAR(32), fetch_type VARCHAR(20) NOT NULL,
        max_fetch_frequency VARCHAR(32), data_life VARCHAR(32), awe_request_id VARCHAR(64),
        effective_from TIMESTAMPTZ, CONSTRAINT uq_partner_policy_version UNIQUE (partner_id, version))""",
    """CREATE TABLE consent_requests (
        id VARCHAR PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ,
        subject_id_type VARCHAR(50) NOT NULL, subject_id_value VARCHAR(255) NOT NULL,
        controller_id VARCHAR(255) NOT NULL, partner_id VARCHAR NOT NULL, purpose JSONB NOT NULL,
        requested_scopes JSONB NOT NULL, valid_from TIMESTAMPTZ, valid_until TIMESTAMPTZ,
        status VARCHAR(20) NOT NULL)""",
]
NEW_REQUEST_COLUMNS = {
    "method", "created_by", "verification_note", "verified_at", "otp_hash", "otp_expires_at",
    "otp_attempts", "otp_verified_at", "otp_channel", "otp_destination", "otp_provider",
    "otp_reference", "otp_send_count", "otp_last_sent_at", "otp_failures_total",
}


async def _columns(table):
    from openg2p_fastapi_common.context import dbengine
    from sqlalchemy import text

    async with dbengine.get().begin() as conn:
        rows = await conn.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
            {"t": table},
        )
        return {r[0] for r in rows}


@pytest.fixture(scope="module")
def upgraded(env, loop):
    """Reset to the untouched 1.0 shape with one legacy row each, then migrate TWICE."""
    from openg2p_fastapi_common.context import dbengine
    from sqlalchemy import text

    now = datetime.now(timezone.utc)

    async def reset():
        async with dbengine.get().begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            for ddl in LEGACY_DDL:
                await conn.execute(text(ddl))
            await conn.execute(
                text("INSERT INTO partners (id, created_at, name, audience, controller_id, status) "
                     "VALUES ('legacy-1', :n, 'Legacy', 'legacy-bank', 'farmer-registry', 'active')"),
                {"n": now})
            await conn.execute(
                text("INSERT INTO consent_requests (id, created_at, subject_id_type, "
                     "subject_id_value, controller_id, partner_id, purpose, requested_scopes, status) "
                     "VALUES ('legacy-req', :n, 'FAYDA_FAN', 'S1', 'farmer-registry', 'legacy-1', "
                     "'{\"code\":\"x\"}', '[\"a\"]', 'pending')"),
                {"n": now})

    loop.run_until_complete(reset())
    # The app's migrate runs its coroutine with asyncio.run; run it on our loop so
    # pooled connections stay bound to one loop.
    with mock.patch("asyncio.run", loop.run_until_complete):
        env["init"].migrate_database(None)
        first = loop.run_until_complete(_columns("consent_requests"))
        env["init"].migrate_database(None)  # idempotent: a second run changes nothing
        second = loop.run_until_complete(_columns("consent_requests"))
    return first, second


def test_upgrade_adds_the_columns_and_is_idempotent(upgraded, loop):
    first, second = upgraded
    assert NEW_REQUEST_COLUMNS <= first and first == second
    assert "consent_method" in loop.run_until_complete(_columns("partner_policies"))


def test_upgrade_keeps_the_legacy_rows(upgraded, loop):
    from openg2p_fastapi_common.context import dbengine
    from sqlalchemy import text

    async def read():
        async with dbengine.get().begin() as conn:
            req = (await conn.execute(text(
                "SELECT status, method, otp_hash FROM consent_requests WHERE id='legacy-req'"
            ))).one()
            n = (await conn.execute(text("SELECT count(*) FROM partners"))).scalar()
            return req, n

    req, n = loop.run_until_complete(read())
    assert tuple(req) == ("pending", None, None) and n == 1


def test_concurrent_verify_of_the_same_code_grants_once(
    upgraded, new_bank, as_partner, codes, otp_config, run  # noqa: F811
):
    from openg2p_consent_manager.db import async_session
    from openg2p_consent_manager.models import ConsentArtefact, ConsentReceipt
    from openg2p_consent_manager.services import LifecycleError, OtpConsentService
    from sqlalchemy import func, select

    bank = new_bank(consent_method="otp")
    pc = as_partner(bank.audience)
    status, req = pc("POST", PORTAL_REQUESTS, json=otp_body())
    assert status == 201, req
    code = codes[-1]
    service = OtpConsentService.get_component()

    async def attempt():
        try:
            await service.verify_otp(req["id"], bank.audience, "tester", code)
            return "granted"
        except LifecycleError as exc:
            return exc.status_code

    async def race():
        return await asyncio.gather(*(attempt() for _ in range(8)))

    results = run(race())
    assert results.count("granted") == 1, results
    assert all(r == 409 for r in results if r != "granted"), results

    async def counts():
        async with async_session()() as session:
            artefacts = (await session.execute(
                select(func.count(ConsentArtefact.id)).where(ConsentArtefact.partner_id == bank.id)
            )).scalar()
            receipts = (await session.execute(
                select(func.count(ConsentReceipt.id))
                .join(ConsentArtefact, ConsentArtefact.id == ConsentReceipt.consent_id)
                .where(ConsentArtefact.partner_id == bank.id)
            )).scalar()
            return artefacts, receipts

    assert run(counts()) == (1, 1)
