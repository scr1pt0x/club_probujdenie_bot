import asyncio
import importlib
from datetime import timedelta

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from test_postgres_safety import URL, database_scenario

from bot.db.models import Flow, FreePromoUse, Membership, Payment, PromoCode, UserPromo
from bot.repositories.promos import delete_user_promos
from bot.services.promos import apply_promo_to_price, record_free_promo_use

pytestmark = pytest.mark.skipif(not URL, reason="Disposable TEST_DATABASE_URL required")


async def setup_free(session, ids, now):
    payment = await session.get(Payment, ids.payment)
    payment.amount_rub = 0
    payment.provider = "promo"
    flow = await session.get(Flow, payment.flow_id)
    flow.sales_open_at, flow.sales_close_at = (
        now - timedelta(days=4),
        now + timedelta(days=4),
    )
    session.add(
        PromoCode(
            code="ONEFLOW",
            kind="free",
            value_int=0,
            active=True,
            starts_at=now - timedelta(days=60),
            ends_at=now + timedelta(days=60),
        )
    )
    session.add(
        UserPromo(user_id=ids.user, code="ONEFLOW", applied_at=now - timedelta(days=1))
    )
    await session.commit()
    return payment, flow


def test_free_code_cannot_pay_for_another_flow_even_after_reset():
    async def scenario(sessions, ids, now):
        async with sessions() as session:
            payment, flow = await setup_free(session, ids, now)
            assert await apply_promo_to_price(session, ids.user, 1990) == 0
            assert await record_free_promo_use(session, payment)
            payment.status = "paid"
            await session.commit()
            assert await apply_promo_to_price(session, ids.user, 1990) == 0
            flow.sales_close_at = now - timedelta(seconds=1)
            next_flow = Flow(
                title="Next",
                start_at=now + timedelta(days=7),
                end_at=now + timedelta(days=42),
                duration_weeks=5,
                is_free=False,
                sales_open_at=now - timedelta(days=1),
                sales_close_at=now + timedelta(days=14),
            )
            session.add(next_flow)
            await session.commit()
            assert await apply_promo_to_price(session, ids.user, 1990) == 1990
            await delete_user_promos(session, ids.user)
            session.add(UserPromo(user_id=ids.user, code="ONEFLOW", applied_at=now))
            await session.commit()
            assert await apply_promo_to_price(session, ids.user, 1990) == 1990
            new_payment = Payment(
                user_id=ids.user,
                flow_id=next_flow.id,
                provider="promo",
                amount_rub=0,
                status="pending",
            )
            session.add(new_payment)
            await session.flush()
            assert not await record_free_promo_use(session, new_payment)
            await session.rollback()
        async with sessions() as session:
            use = await session.get(FreePromoUse, (ids.user, "ONEFLOW"))
            assert use.payment_id == ids.payment
            assert (await session.get(Payment, ids.payment)).status == "paid"
            assert (await session.get(Membership, ids.member)).status == "active"

    asyncio.run(database_scenario(scenario))


def test_unsuccessful_transaction_does_not_consume_free_promo():
    async def scenario(sessions, ids, now):
        async with sessions() as session:
            payment, _ = await setup_free(session, ids, now)
            assert await record_free_promo_use(session, payment)
            await session.rollback()
        async with sessions() as session:
            assert await session.get(FreePromoUse, (ids.user, "ONEFLOW")) is None
            assert await apply_promo_to_price(session, ids.user, 1990) == 0

    asyncio.run(database_scenario(scenario))


def test_expired_free_code_cannot_be_consumed_between_price_and_confirmation():
    async def scenario(sessions, ids, now):
        async with sessions() as session:
            payment, _ = await setup_free(session, ids, now)
            assert await apply_promo_to_price(session, ids.user, 1990) == 0
            (await session.get(PromoCode, "ONEFLOW")).ends_at = now - timedelta(
                seconds=1
            )
            await session.commit()
            assert not await record_free_promo_use(session, payment)
            assert await session.get(FreePromoUse, (ids.user, "ONEFLOW")) is None

    asyncio.run(database_scenario(scenario))


def test_legacy_backfill_binds_first_use_without_revoking_later_access():
    async def scenario(sessions, ids, now):
        async with sessions() as session:
            payment, flow = await setup_free(session, ids, now)
            payment.status = "paid"
            payment.paid_at = now - timedelta(hours=20)
            next_flow = Flow(
                title="Already granted next flow",
                start_at=now + timedelta(days=7),
                end_at=now + timedelta(days=42),
                duration_weeks=5,
                is_free=False,
                sales_open_at=now - timedelta(days=1),
                sales_close_at=now + timedelta(days=14),
            )
            session.add(next_flow)
            await session.flush()
            second = Payment(
                user_id=ids.user,
                flow_id=next_flow.id,
                provider="promo",
                amount_rub=0,
                status="paid",
                paid_at=now,
            )
            session.add(second)
            session.add(
                Membership(
                    user_id=ids.user,
                    flow_id=next_flow.id,
                    status="active",
                    access_start_at=next_flow.start_at,
                    access_end_at=next_flow.end_at,
                    grace_end_at=next_flow.end_at + timedelta(days=1),
                )
            )
            await session.commit()
            before = list(
                (
                    await session.scalars(
                        select(Payment.id).where(Payment.status == "paid")
                    )
                ).all()
            )
            await session.execute(text("DROP TABLE free_promo_uses"))
            connection = await session.connection()
            migration = importlib.import_module(
                "migrations.versions.0011_free_promo_uses"
            )

            def upgrade(conn):
                with Operations.context(MigrationContext.configure(conn)):
                    migration.upgrade()

            await connection.run_sync(upgrade)
            await session.commit()
            use = await session.get(FreePromoUse, (ids.user, "ONEFLOW"))
            assert use.payment_id == ids.payment
            assert use.flow_id == flow.id
            after = list(
                (
                    await session.scalars(
                        select(Payment.id).where(Payment.status == "paid")
                    )
                ).all()
            )
            assert before == after
            assert (await session.get(Membership, ids.member)).status == "active"
            assert list((await session.scalars(select(Membership.status))).all()) == [
                "active",
                "active",
            ]

    asyncio.run(database_scenario(scenario))
