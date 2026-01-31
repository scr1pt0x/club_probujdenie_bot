from bot.db.models import PaymentStatus
from bot.payments.adapter import PaymentAdapter


class DummyPaymentAdapter(PaymentAdapter):
    async def get_payment_status(self, external_id: str) -> PaymentStatus:
        # TODO: подключить реальный платежный провайдер.
        return PaymentStatus.PENDING

    async def create_payment(self, amount_rub: int, description: str) -> str:
        # TODO: вернуть внешний идентификатор платежа от провайдера.
        raise NotImplementedError
