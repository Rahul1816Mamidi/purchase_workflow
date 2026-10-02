import json

import pytest

from app.model_adapter import MockModelAdapter
from app.models import PurchaseRequest
from app.workflow import PurchaseWorkflow


def make_request(
    request_id="PR-001",
    vendor_id="V-101",
    quantity=10,
    unit_price=800,
    currency="USD",
):
    return PurchaseRequest(
        request_id=request_id,
        vendor_id=vendor_id,
        description="Laptops",
        quantity=quantity,
        unit_price=unit_price,
        currency=currency,
    )


def test_valid_request_then_human_approval(storage):
    workflow = PurchaseWorkflow(storage, MockModelAdapter("valid"))

    result = workflow.submit(make_request())

    assert result["state"] == "PENDING_REVIEW"
    assert result["total"] == 8000
    assert result["model_recommendation"]["recommendation"] == "approve"

    result = workflow.review(
        "PR-001",
        reviewer="Rahul",
        decision="approve",
        note="Verified",
    )

    assert result["state"] == "APPROVED"
    assert result["human_review"]["reviewer"] == "Rahul"


def test_insufficient_budget_blocks_positive_model_recommendation(storage):
    workflow = PurchaseWorkflow(storage, MockModelAdapter("valid"))

    result = workflow.submit(
        make_request(quantity=20, unit_price=800)
    )

    assert result["state"] == "BLOCKED"
    assert "insufficient_budget" in result["deterministic_findings"]
    assert result["model_recommendation"]["recommendation"] == "approve"


def test_unknown_vendor_blocks_request(storage):
    workflow = PurchaseWorkflow(storage, MockModelAdapter("valid"))

    result = workflow.submit(
        make_request(vendor_id="V-999")
    )

    assert result["state"] == "BLOCKED"
    assert "vendor_not_found" in result["deterministic_findings"]


def test_inactive_vendor_blocks_request(storage):
    workflow = PurchaseWorkflow(storage, MockModelAdapter("valid"))

    result = workflow.submit(
        make_request(vendor_id="V-102")
    )

    assert result["state"] == "BLOCKED"
    assert "vendor_inactive" in result["deterministic_findings"]


def test_malformed_model_output_fails(storage):
    workflow = PurchaseWorkflow(storage, MockModelAdapter("malformed"))

    result = workflow.submit(make_request())

    assert result["state"] == "FAILED"
    assert "MODEL_VALIDATION_FAILED" in result["errors"]


def test_unsupported_model_claim_fails(storage):
    workflow = PurchaseWorkflow(
        storage, MockModelAdapter("unsupported_claim")
    )

    result = workflow.submit(make_request())

    assert result["state"] == "FAILED"
    assert "MODEL_VALIDATION_FAILED" in result["errors"]


def test_temporary_timeout_retries_then_succeeds(storage):
    adapter = MockModelAdapter("temporary_timeout")
    workflow = PurchaseWorkflow(storage, adapter)

    result = workflow.submit(make_request())

    assert result["state"] == "PENDING_REVIEW"
    assert adapter.calls == 2
    assert any(
        event["event_type"] == "MODEL_RETRY"
        for event in result["events"]
    )


def test_persistent_failure_is_bounded(storage):
    adapter = MockModelAdapter("persistent_failure")
    workflow = PurchaseWorkflow(storage, adapter)

    result = workflow.submit(make_request())

    assert result["state"] == "FAILED"
    assert adapter.calls == 1
    assert any(
        event["event_type"] == "MODEL_FAILURE"
        for event in result["events"]
    )


def test_same_request_resumes_without_repeating_saved_tools(storage):
    adapter = MockModelAdapter("valid")
    workflow = PurchaseWorkflow(storage, adapter)

    first = workflow.submit(make_request())
    assert first["state"] == "PENDING_REVIEW"

    second = workflow.submit(make_request())

    assert second["state"] == "PENDING_REVIEW"
    duplicate_events = [
        event
        for event in second["events"]
        if event["event_type"] == "DUPLICATE_SUBMISSION"
    ]
    assert duplicate_events


def test_changed_content_with_same_request_id_is_rejected(storage):
    workflow = PurchaseWorkflow(storage, MockModelAdapter("valid"))

    workflow.submit(make_request())

    changed = make_request(quantity=11)

    result = workflow.submit(changed)

    assert result["errors"] == [
        "request_id_already_exists_with_changed_content"
    ]
    assert result["state"] == "PENDING_REVIEW"


def test_resume_after_saved_tool_results(storage):
    adapter = MockModelAdapter("valid")
    workflow = PurchaseWorkflow(storage, adapter)

    request = make_request()
    storage.create_request(request.to_dict(), "RECEIVED")

    from app.tools import lookup_budget, lookup_vendor
    vendor = lookup_vendor(request.vendor_id)
    budget = lookup_budget(request.currency)

    storage.update_request(
        request.request_id,
        vendor_result_json=vendor,
        budget_result_json=budget,
    )

    storage.add_event(
        request.request_id,
        "VENDOR_LOOKUP_COMPLETED",
        vendor,
    )
    storage.add_event(
        request.request_id,
        "BUDGET_LOOKUP_COMPLETED",
        budget,
    )

    result = workflow.submit(request)

    assert result["state"] == "PENDING_REVIEW"
    vendor_events = [
        e for e in result["events"]
        if e["event_type"] == "VENDOR_LOOKUP_COMPLETED"
    ]
    budget_events = [
        e for e in result["events"]
        if e["event_type"] == "BUDGET_LOOKUP_COMPLETED"
    ]

    assert len(vendor_events) == 1
    assert len(budget_events) == 1


def test_review_requires_pending_state(storage):
    workflow = PurchaseWorkflow(storage, MockModelAdapter("valid"))

    workflow.submit(
        make_request(quantity=20, unit_price=800)
    )

    with pytest.raises(ValueError):
        workflow.review(
            "PR-001",
            reviewer="Rahul",
            decision="approve",
            note="Attempted approval",
        )


def test_currency_mismatch_blocks_request(storage):
    workflow = PurchaseWorkflow(storage, MockModelAdapter("valid"))

    result = workflow.submit(
        make_request(currency="EUR")
    )

    assert result["state"] == "BLOCKED"
    assert "currency_mismatch" in result["deterministic_findings"]
