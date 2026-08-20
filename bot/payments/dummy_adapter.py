from bot.payments.adapter import PaymentAdapter


class DummyPaymentAdapter(PaymentAdapter):
    async def get_payment(self, external_id: str) -> dict:
        return {"id": external_id, "status": "pending"}

    async def create_payment(
        self,
        amount_rub: int,
        description: str,
        metadata: dict,
        internal_payment_id: int,
    ) -> tuple[str, str]:
        raise NotImplementedError
