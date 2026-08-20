import logging
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from bot.db.models import PaymentStatus
from bot.db.session import AsyncSessionLocal
from bot.payments.verification import validate_remote_payment
from bot.payments.yookassa_adapter import YooKassaAdapter
from bot.repositories.payments import get_payment_by_external_id
from bot.services.payments import confirm_payment, notify_payment_status

logger = logging.getLogger(__name__)


def create_app(bot) -> FastAPI:
    app = FastAPI()
    adapter = YooKassaAdapter()

    @app.get("/api/healthz")
    async def healthcheck() -> JSONResponse:
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            logger.exception("Healthcheck database query failed")
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.post("/api/yookassa/webhook")
    async def yookassa_webhook(request: Request) -> Response:
        try:
            payload = await request.json()
        except ValueError:
            logger.warning("YooKassa webhook received invalid JSON")
            return Response(status_code=400)
        if not isinstance(payload, dict):
            return Response(status_code=400)
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
                PaymentStatus.NEEDS_REVIEW,
            }:
                return Response(status_code=200)
            if event == "payment.canceled" and payment.status in {
                PaymentStatus.FAILED,
                PaymentStatus.EXPIRED,
            }:
                return Response(status_code=200)

            if event in {"payment.succeeded", "payment.canceled"}:
                try:
                    remote = await adapter.get_payment(payment_id)
                except Exception as exc:
                    logger.exception("Failed to verify payment", exc_info=exc)
                    # Ask YooKassa to retry instead of acknowledging an event
                    # that could not be verified.
                    return Response(status_code=503)
                expected_status = (
                    "succeeded" if event == "payment.succeeded" else "canceled"
                )
                if remote.get("status") != expected_status:
                    return Response(status_code=200)
                validation_error = validate_remote_payment(
                    remote,
                    external_id=payment_id,
                    internal_payment_id=payment.id,
                    user_id=payment.user_id,
                    amount_rub=payment.amount_rub,
                    currency=payment.currency,
                )
                if validation_error:
                    logger.warning(
                        "Payment verification mismatch",
                        extra={
                            "payment_id": payment_id,
                            "local_payment_id": payment.id,
                            "local_user_id": payment.user_id,
                            "reason": validation_error,
                        },
                    )
                    return Response(status_code=200)

            if event == "payment.succeeded":
                await confirm_payment(
                    session, bot, payment, paid_at=datetime.now(timezone.utc)
                )
                await session.commit()
                return Response(status_code=200)

            if event == "payment.canceled":
                payment.status = PaymentStatus.FAILED
                deadline = payment.expires_at or payment.created_at + timedelta(
                    hours=24
                )
                if deadline >= datetime.now(timezone.utc) - timedelta(days=1):
                    await notify_payment_status(
                        session,
                        bot,
                        payment.user_id,
                        "payment_failed",
                        dedupe_key=f"payment:{payment.id}:payment_failed",
                    )
                await session.commit()
                return Response(status_code=200)

        return Response(status_code=200)

    return app
