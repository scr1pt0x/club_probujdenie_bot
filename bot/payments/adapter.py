from abc import ABC, abstractmethod

from bot.db.models import PaymentStatus


class PaymentAdapter(ABC):
    @abstractmethod
    async def get_payment_status(self, external_id: str) -> PaymentStatus:
        raise NotImplementedError

    @abstractmethod
    async def create_payment(self, amount_rub: int, description: str) -> str:
        """
        Возвращает external_id платежа.
        """
        raise NotImplementedError
