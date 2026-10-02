import hashlib
import json
from typing import Any, Callable

from .model_adapter import (
    ModelAdapter,
    PersistentModelError,
    TemporaryModelError,
    validate_model_response,
)
from .models import PurchaseRequest
from .storage import Storage
from .tools import lookup_budget, lookup_vendor
from .validation import deterministic_checks, validate_request


MAX_MODEL_RETRIES = 2


def _fingerprint(request: PurchaseRequest) -> str:
    raw = json.dumps(request.to_dict(), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PurchaseWorkflow:
    def __init__(
        self,
        storage: Storage,
        adapter: ModelAdapter,
        progress: Callable[[str], None] | None = None,
    ):
        self.storage = storage
        self.adapter = adapter
        self.progress = progress or (lambda _message: None)

    def submit(self, request: PurchaseRequest) -> dict[str, Any]:
        existing = self.storage.get_request(request.request_id)

        if existing:
            old_content = existing["content_json"]
            if old_content != request.to_dict():
                self.storage.add_event(
                    request.request_id,
                    "CONFLICTING_RESUBMISSION",
                    {"message": "Same request ID was submitted with changed content."},
                )
                return self.get_result(
                    request.request_id,
                    extra_error="request_id_already_exists_with_changed_content",
                )

            if existing["state"] != "RECEIVED":
                self.storage.add_event(
                    request.request_id,
                    "DUPLICATE_SUBMISSION",
                    {"message": "Existing request reused."},
                )
                return self.get_result(request.request_id)
        else:
            self.storage.create_request(request.to_dict(), "RECEIVED")
            self.storage.add_event(
                request.request_id,
                "REQUEST_RECEIVED",
                {"fingerprint": _fingerprint(request)},
            )

        self.progress("Validating purchase request...")
        input_errors = validate_request(request)
        if input_errors:
            self.storage.update_request(
                request.request_id,
                state="BLOCKED",
                deterministic_findings_json=input_errors,
            )
            self.storage.add_event(
                request.request_id,
                "VALIDATION_FAILED",
                {"findings": input_errors},
            )
            return self.get_result(request.request_id)

        existing = self.storage.get_request(request.request_id)

        if existing["vendor_result"] is None:
            self.progress("Checking vendor...")
            vendor = lookup_vendor(request.vendor_id)
            self.storage.update_request(
                request.request_id,
                vendor_result_json=vendor,
            )
            self.storage.add_event(
                request.request_id,
                "VENDOR_LOOKUP_COMPLETED",
                vendor,
            )
        else:
            self.progress("Checking vendor... saved result found, reusing it.")
            vendor = existing["vendor_result"]

        existing = self.storage.get_request(request.request_id)

        if existing["budget_result"] is None:
            self.progress("Checking budget...")
            budget = lookup_budget(request.currency)
            self.storage.update_request(
                request.request_id,
                budget_result_json=budget,
            )
            self.storage.add_event(
                request.request_id,
                "BUDGET_LOOKUP_COMPLETED",
                budget,
            )
        else:
            self.progress("Checking budget... saved result found, reusing it.")
            budget = existing["budget_result"]

        self.progress("Running deterministic checks...")
        total, findings = deterministic_checks(request, vendor, budget)

        self.storage.update_request(
            request.request_id,
            total=total,
            deterministic_findings_json=findings,
        )
        self.storage.add_event(
            request.request_id,
            "DETERMINISTIC_CHECKS_COMPLETED",
            {"total": total, "findings": findings},
        )

        # The model is allowed to make a recommendation even when a
        # deterministic issue exists, so the workflow can demonstrate that
        # deterministic rules cannot be overridden by the model.
        existing = self.storage.get_request(request.request_id)

        if existing["model_result"] is None:
            self.progress("Getting model recommendation...")
            tool_results = {
                "vendor": vendor,
                "budget": budget,
                "total": total,
                "findings": findings,
            }

            for attempt in range(1, MAX_MODEL_RETRIES + 1):
                try:
                    response = self.adapter.recommend(
                        request.to_dict(),
                        tool_results,
                    )

                    valid, error = validate_model_response(
                        response,
                        request.to_dict(),
                        tool_results,
                    )

                    if not valid:
                        self.storage.update_request(
                            request.request_id,
                            state="FAILED",
                        )
                        self.storage.add_event(
                            request.request_id,
                            "MODEL_VALIDATION_FAILED",
                            {"error": error},
                        )
                        return self.get_result(request.request_id)

                    model_result = response.to_dict()
                    self.storage.update_request(
                        request.request_id,
                        model_result_json=model_result,
                    )
                    self.storage.add_event(
                        request.request_id,
                        "MODEL_RECOMMENDATION_COMPLETED",
                        model_result,
                    )
                    break

                except TemporaryModelError as exc:
                    self.storage.add_event(
                        request.request_id,
                        "MODEL_RETRY",
                        {"attempt": attempt, "error": str(exc)},
                    )
                    if attempt == MAX_MODEL_RETRIES:
                        self.storage.update_request(
                            request.request_id,
                            state="FAILED",
                        )
                        self.storage.add_event(
                            request.request_id,
                            "MODEL_RETRY_LIMIT_REACHED",
                            {"attempts": attempt},
                        )
                        return self.get_result(request.request_id)

                except PersistentModelError as exc:
                    self.storage.add_event(
                        request.request_id,
                        "MODEL_FAILURE",
                        {"error": str(exc)},
                    )
                    self.storage.update_request(
                        request.request_id,
                        state="FAILED",
                    )
                    return self.get_result(request.request_id)

        # Deterministic findings always win over a model recommendation.
        if findings:
            self.storage.update_request(
                request.request_id,
                state="BLOCKED",
            )
            self.storage.add_event(
                request.request_id,
                "REQUEST_BLOCKED",
                {
                    "findings": findings,
                    "reason": "deterministic_checks_failed",
                },
            )
            return self.get_result(request.request_id)

        self.storage.update_request(
            request.request_id,
            state="PENDING_REVIEW",
        )
        return self.get_result(request.request_id)

    def review(
        self,
        request_id: str,
        reviewer: str,
        decision: str,
        note: str,
    ) -> dict[str, Any]:
        record = self.storage.get_request(request_id)

        if record is None:
            raise ValueError("Request not found")

        if record["state"] != "PENDING_REVIEW":
            raise ValueError(
                f"Request is not pending review. Current state: {record['state']}"
            )

        if decision not in {"approve", "reject"}:
            raise ValueError("Decision must be approve or reject")

        final_state = "APPROVED" if decision == "approve" else "REJECTED"

        review_data = {
            "reviewer": reviewer,
            "decision": decision,
            "note": note,
        }

        self.storage.update_request(
            request_id,
            state=final_state,
            human_review_json=review_data,
        )
        self.storage.add_event(
            request_id,
            "HUMAN_REVIEW_COMPLETED",
            review_data,
        )

        return self.get_result(request_id)

    def get_result(
        self, request_id: str, extra_error: str | None = None
    ) -> dict[str, Any]:
        record = self.storage.get_request(request_id)
        if record is None:
            raise ValueError("Request not found")

        content = record["content_json"]

        result = {
            "request_id": request_id,
            "state": record["state"],
            "purchase_request": content,
            "total": record["total"],
            "tool_results": {
                "vendor": record["vendor_result"],
                "budget": record["budget_result"],
            },
            "deterministic_findings": record["deterministic_findings"],
            "model_recommendation": record["model_result"],
            "human_review": record["human_review"],
            "errors": [],
            "events": self.storage.events(request_id),
        }

        if extra_error:
            result["errors"].append(extra_error)

        for event in result["events"]:
            if "error" in event["details"]:
                result["errors"].append(event["details"]["error"])
            if event["event_type"] in {
                "VALIDATION_FAILED",
                "MODEL_VALIDATION_FAILED",
                "MODEL_RETRY_LIMIT_REACHED",
            }:
                result["errors"].append(event["event_type"])

        return result
