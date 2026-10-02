from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PurchaseRequest:
    request_id: str
    vendor_id: str
    description: str
    quantity: int
    unit_price: float
    currency: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "vendor_id": self.vendor_id,
            "description": self.description,
            "quantity": self.quantity,
            "unit_price": self.unit_price,
            "currency": self.currency,
        }

    def fingerprint_data(self) -> tuple:
        return (
            self.request_id,
            self.vendor_id,
            self.description,
            self.quantity,
            self.unit_price,
            self.currency,
        )


@dataclass
class ModelRecommendation:
    recommendation: str
    explanation: str

    def to_dict(self) -> dict[str, str]:
        return {
            "recommendation": self.recommendation,
            "explanation": self.explanation,
        }
