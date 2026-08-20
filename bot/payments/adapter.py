from abc import ABC, abstractmethod


class PaymentAdapter(ABC):
    @abstractmethod
    async def get_payment(self, external_id: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    async def create_payment(
        self,
        amount_rub: int,
        description: str,
        metadata: dict,
        internal_payment_id: int,
    ) -> tuple[str, str]:
        raise NotImplementedError
