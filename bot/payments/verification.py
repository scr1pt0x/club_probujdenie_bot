from decimal import Decimal, InvalidOperation


def validate_remote_payment(
    remote: dict,
    *,
    external_id: str,
    internal_payment_id: int,
    user_id: int,
    amount_rub: int,
    currency: str = "RUB",
) -> str | None:
    """Return a diagnostic code if provider data does not match the order."""
    if remote.get("id") != external_id:
        return "external_id_mismatch"

    metadata = remote.get("metadata")
    if not isinstance(metadata, dict):
        return "metadata_missing"
    try:
        remote_internal_id = int(metadata["internal_payment_id"])
        remote_user_id = int(metadata["user_id"])
    except (KeyError, TypeError, ValueError):
        return "metadata_invalid"
    if remote_internal_id != internal_payment_id:
        return "internal_payment_id_mismatch"
    if remote_user_id != user_id:
        return "user_id_mismatch"

    amount = remote.get("amount")
    if not isinstance(amount, dict):
        return "amount_missing"
    if amount.get("currency") != currency:
        return "currency_mismatch"
    try:
        remote_amount = Decimal(str(amount["value"])).quantize(Decimal("0.00"))
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return "amount_invalid"
    expected_amount = Decimal(amount_rub).quantize(Decimal("0.00"))
    if remote_amount != expected_amount:
        return "amount_mismatch"
    return None
