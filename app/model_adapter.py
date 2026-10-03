import json
import re
from abc import ABC, abstractmethod
from typing import Any

from .models import ModelRecommendation


class TemporaryModelError(Exception):
    pass


class PersistentModelError(Exception):
    pass


class SimulatedCrash(Exception):
    """Raised by the mock to imitate the process dying. The workflow must NOT catch it."""


class ModelAdapter(ABC):
    @abstractmethod
    def recommend(
        self, request: dict[str, Any], tool_results: dict[str, Any]
    ) -> ModelRecommendation:
        raise NotImplementedError


class MockModelAdapter(ModelAdapter):
    """
    Deterministic adapter for local execution and automated tests.

    Scenarios:
    valid
    malformed
    unsupported_claim
    temporary_timeout
    persistent_failure
    crash_after_tools  (first call raises SimulatedCrash, i.e. the process dies
                        after vendor/budget results were saved)
    """

    def __init__(self, scenario: str = "valid"):
        self.scenario = scenario
        self.calls = 0

    def recommend(
        self, request: dict[str, Any], tool_results: dict[str, Any]
    ) -> ModelRecommendation:
        self.calls += 1

        if self.scenario == "crash_after_tools" and self.calls == 1:
            raise SimulatedCrash("Process crashed after tool results were saved")

        if self.scenario == "temporary_timeout" and self.calls == 1:
            raise TemporaryModelError("Temporary model timeout")

        if self.scenario == "persistent_failure":
            raise PersistentModelError("Model service unavailable")

        if self.scenario == "malformed":
            return json.loads('{"recommendation": "approve"}')

        if self.scenario == "unsupported_claim":
            return ModelRecommendation(
                recommendation="approve",
                explanation=(
                    "The vendor has worked with the company for 10 years, "
                    "and the purchase is within the supplied budget."
                ),
            )

        return ModelRecommendation(
            recommendation="approve",
            explanation=(
                "Based on the supplied request information, I recommend approval."
            ),
        )


def validate_model_response(
    response: Any,
    request: dict[str, Any],
    tool_results: dict[str, Any],
) -> tuple[bool, str]:
    if not isinstance(response, ModelRecommendation):
        return False, "model_output_malformed"

    if response.recommendation not in {"approve", "reject"}:
        return False, "invalid_recommendation"

    if not isinstance(response.explanation, str) or not response.explanation.strip():
        return False, "missing_explanation"

    explanation = response.explanation.lower()

    vendor = tool_results["vendor"]
    budget = tool_results["budget"]
    total = tool_results["total"]

    # Small deterministic claim checks. This intentionally does not attempt
    # complete natural-language fact verification.
    unsupported_patterns = [
        ("10 years", "unsupported_vendor_history"),
        ("ten years", "unsupported_vendor_history"),
    ]

    for phrase, error in unsupported_patterns:
        if phrase in explanation:
            return False, error

    if "within the supplied budget" in explanation:
        if total > budget["available"]:
            return False, "unsupported_budget_claim"

    if "vendor checks passed" in explanation:
        if not (
            vendor["exists"]
            and vendor["active"]
            and vendor["permitted_currency"] == request["currency"]
        ):
            return False, "unsupported_vendor_claim"

    # Every number in the explanation must be one we supplied: quantity, unit
    # price, total or available budget. The request's own IDs are removed first
    # so "vendor V-101" is fine but an invented "V-777" leaves 777 behind.
    allowed = [
        float(request["quantity"]),
        float(request["unit_price"]),
        float(total),
        float(budget["available"]),
    ]
    text = response.explanation
    for known_id in (request["vendor_id"], request["request_id"]):
        text = re.sub(re.escape(known_id), " ", text, flags=re.IGNORECASE)

    for token in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
        number = float(token.replace(",", ""))
        if not any(abs(number - value) < 0.005 for value in allowed):
            return False, "unsupported_number_claim"

    return True, ""
