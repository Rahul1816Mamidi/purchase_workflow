from typing import Any
from .models import PurchaseRequest


REQUIRED_FIELDS = (
    "request_id",
    "vendor_id",
    "description",
    "quantity",
    "unit_price",
    "currency",
)


def validate_request(request: PurchaseRequest) -> list[str]:
    errors: list[str] = []

    for field in REQUIRED_FIELDS:
        value = getattr(request, field)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f"missing_{field}")

    if not isinstance(request.quantity, int) or isinstance(request.quantity, bool):
        errors.append("quantity_must_be_integer")
    elif request.quantity <= 0:
        errors.append("quantity_must_be_positive")

    if not isinstance(request.unit_price, (int, float)) or isinstance(
        request.unit_price, bool
    ):
        errors.append("unit_price_must_be_number")
    elif request.unit_price <= 0:
        errors.append("unit_price_must_be_positive")

    if isinstance(request.currency, str) and request.currency:
        if request.currency != request.currency.upper():
            errors.append("currency_must_be_uppercase")

    return errors


def deterministic_checks(
    request: PurchaseRequest,
    vendor: dict[str, Any],
    budget: dict[str, Any],
) -> tuple[float, list[str]]:
    total = round(request.quantity * request.unit_price, 2)
    findings: list[str] = []

    if not vendor["exists"]:
        findings.append("vendor_not_found")
    elif not vendor["active"]:
        findings.append("vendor_inactive")

    if vendor["exists"] and vendor["permitted_currency"] != request.currency:
        findings.append("currency_mismatch")

    if budget["currency"] != request.currency:
        findings.append("budget_currency_unavailable")
    elif total > budget["available"]:
        findings.append("insufficient_budget")

    return total, findings
