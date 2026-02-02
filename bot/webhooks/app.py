from datetime import datetime, timezone
from decimal import Decimal
import logging

from fastapi import FastAPI, Request, Response

from bot.db.session import AsyncSessionLocal
from bot.db.models import PaymentStatus
from bot.payments.yookassa_adapter import YooKassaAdapter
from bot.repositories.payments import get_payment_by_external_id
from bot.services.payments import confirm_payment
from config import settings


logger = logging.getLogger(__name__)


def create_app(bot) -> FastAPI:
    app = FastAPI()
    adapter = YooKassaAdapter()

    @app.post("/api/yookassa/webhook")
    async def yookassa_webhook(request: Request) -> Response:
        payload = await request.json()
        event = payload.get("event")
        obj = payload.get("object") or {}
        payment_id = obj.get("id")
        if not payment_id:
            return Response(status_code=200)

        async with AsyncSessionLocal() as session:
            payment = await get_payment_by_external_id(session, payment_id)
            if not payment:
                return Response(status_code=200)

            if payment.status in {
                PaymentStatus.PAID,
                PaymentStatus.FAILED,
                PaymentStatus.EXPIRED,
            }:
                return Response(status_code=200)

            if event == "payment.succeeded":
                try:
                    remote = await adapter.get_payment(payment_id)
                except Exception as exc:
                    logger.exception("Failed to verify payment", exc_info=exc)
                    return Response(status_code=200)
                if remote.get("status") != "succeeded":
                    return Response(status_code=200)
                try:
                    remote_amount = Decimal(remote["amount"]["value"])
                except Exception as exc:
                    logger.exception("Failed to parse remote amount", exc_info=exc)
                    return Response(status_code=200)
                expected = Decimal(payment.amount_rub).quantize(Decimal("0.00"))
                if remote_amount != expected:
                    logger.warning(
                        "Payment amount mismatch",
                        extra={
                            "payment_id": payment_id,
                            "remote_amount": str(remote_amount),
                            "local_amount": payment.amount_rub,
                        },
                    )
                    return Response(status_code=200)
                await confirm_payment(session, bot, payment, paid_at=datetime.now(timezone.utc))
                await session.commit()
                return Response(status_code=200)

            if event == "payment.canceled":
                payment.status = PaymentStatus.FAILED
                await session.commit()
                return Response(status_code=200)

        return Response(status_code=200)

    return app
