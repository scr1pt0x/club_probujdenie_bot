import base64
import logging
from decimal import Decimal

import httpx

from bot.db.models import PaymentStatus
from config import settings


logger = logging.getLogger(__name__)


class YooKassaAdapter:
    def __init__(self) -> None:
        self._base_url = "https://api.yookassa.ru/v3"

    def _auth_header(self) -> str:
        raw = f"{settings.yookassa_shop_id}:{settings.yookassa_secret_key}"
        token = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
        return f"Basic {token}"

    def _format_amount(self, amount_rub: int) -> str:
        return f"{Decimal(amount_rub):.2f}"

    async def create_payment(
        self, amount_rub: int, description: str, metadata: dict, internal_payment_id: int
    ) -> tuple[str, str]:
        headers = {
            "Authorization": self._auth_header(),
            "Idempotence-Key": f"club_probujdenie:{internal_payment_id}",
        }
        payload = {
            "amount": {"value": self._format_amount(amount_rub), "currency": "RUB"},
            "confirmation": {
                "type": "redirect",
                "return_url": settings.public_base_url,
            },
            "capture": True,
            "description": description,
            "metadata": metadata,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self._base_url}/payments", headers=headers, json=payload
            )
            response.raise_for_status()
            data = response.json()
        payment_id = data["id"]
        confirmation_url = data["confirmation"]["confirmation_url"]
        return payment_id, confirmation_url

    async def get_payment(self, payment_id: str) -> dict:
        headers = {"Authorization": self._auth_header()}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self._base_url}/payments/{payment_id}", headers=headers
            )
            response.raise_for_status()
            return response.json()

    async def get_payment_status(self, external_id: str) -> PaymentStatus:
        data = await self.get_payment(external_id)
        status = data.get("status")
        if status == "succeeded":
            return PaymentStatus.PAID
        if status == "canceled":
            return PaymentStatus.FAILED
        return PaymentStatus.PENDING
