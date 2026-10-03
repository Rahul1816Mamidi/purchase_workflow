import json

import pytest

from app.model_adapter import (
    MockModelAdapter,
    SimulatedCrash,
    validate_model_response,
)
from app.models import ModelRecommendation, PurchaseRequest
from app.storage import Storage
from app.workflow import InvalidReviewInput, PurchaseWorkflow


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


# ---------------------------------------------------------------------------
# Reviewer identity and note are mandatory
# ---------------------------------------------------------------------------


def test_review_rejects_blank_reviewer_or_note(storage):
    workflow = PurchaseWorkflow(storage, MockModelAdapter("valid"))
    workflow.submit(make_request())

    for reviewer, note in (
        ("", "Verified"),
        ("   ", "Verified"),
        ("Rahul", ""),
        ("Rahul", "   "),
    ):
        with pytest.raises(InvalidReviewInput):
            workflow.review("PR-001", reviewer, "approve", note)

    # Nothing was recorded and the request is still waiting for a human.
    record = storage.get_request("PR-001")
    assert record["state"] == "PENDING_REVIEW"
    assert record["human_review"] is None


def test_review_stores_trimmed_identity_and_note(storage):
    workflow = PurchaseWorkflow(storage, MockModelAdapter("valid"))
    workflow.submit(make_request())

    result = workflow.review("PR-001", "  Rahul ", "reject", " Wrong vendor ")

    assert result["human_review"] == {
        "reviewer": "Rahul",
        "decision": "reject",
        "note": "Wrong vendor",
    }


# ---------------------------------------------------------------------------
# Unsupported-claim check: every number must be a supplied number
# ---------------------------------------------------------------------------


def _claim_check(explanation, quantity=2, unit_price=800.0):
    request = make_request(quantity=quantity, unit_price=unit_price).to_dict()
    total = quantity * unit_price
    tool_results = {
        "vendor": {
            "exists": True,
            "active": True,
            "permitted_currency": "USD",
        },
        "budget": {"currency": "USD", "available": 10000.0},
        "total": total,
    }
    return validate_model_response(
        ModelRecommendation("approve", explanation),
        request,
        tool_results,
    )


def test_invented_numbers_are_rejected():
    for text in (
        "The vendor has supplied us for 12 years.",
        "The vendor has 99.5% on-time delivery.",
        "Vendor V-777 is our preferred supplier.",
        "A 15% discount was applied.",
    ):
        assert _claim_check(text) == (False, "unsupported_number_claim"), text


def test_supplied_numbers_and_own_ids_are_accepted():
    for text in (
        "Quantity 2 at 800 gives a total of 1,600.00, within the available 10,000.",
        "Request PR-001 to vendor V-101 totals 1600.",
        "I recommend approval.",
    ):
        assert _claim_check(text) == (True, ""), text


def test_existing_phrase_checks_still_catch_claim_equal_to_quantity():
    # 10 is the quantity here, so the number scan allows it; the original
    # "10 years" phrase check must still reject the claim.
    valid, error = _claim_check(
        "The vendor has worked with us for 10 years.",
        quantity=10,
        unit_price=800.0,
    )
    assert (valid, error) == (False, "unsupported_vendor_history")


# ---------------------------------------------------------------------------
# FAILED requests can be resumed
# ---------------------------------------------------------------------------


def _count_tool_calls(monkeypatch):
    import app.workflow as workflow_module

    calls = {"vendor": 0, "budget": 0}
    real_vendor = workflow_module.lookup_vendor
    real_budget = workflow_module.lookup_budget

    def counted_vendor(vendor_id):
        calls["vendor"] += 1
        return real_vendor(vendor_id)

    def counted_budget(currency):
        calls["budget"] += 1
        return real_budget(currency)

    monkeypatch.setattr(workflow_module, "lookup_vendor", counted_vendor)
    monkeypatch.setattr(workflow_module, "lookup_budget", counted_budget)
    return calls


def test_failed_request_resumes_and_reuses_saved_tool_results(
    storage, monkeypatch
):
    calls = _count_tool_calls(monkeypatch)

    failed = PurchaseWorkflow(
        storage, MockModelAdapter("persistent_failure")
    ).submit(make_request())
    assert failed["state"] == "FAILED"

    adapter = MockModelAdapter("valid")
    resumed = PurchaseWorkflow(storage, adapter).submit(make_request())

    assert resumed["state"] == "PENDING_REVIEW"
    assert adapter.calls == 1
    assert calls == {"vendor": 1, "budget": 1}

    event_types = [event["event_type"] for event in resumed["events"]]
    assert "RESUMED_AFTER_FAILURE" in event_types
    assert "MODEL_FAILURE" in event_types  # failure history is kept
    assert "DUPLICATE_SUBMISSION" not in event_types


# ---------------------------------------------------------------------------
# A blocked request stays BLOCKED when the model step fails
# ---------------------------------------------------------------------------


def test_blocked_request_stays_blocked_when_model_output_is_bad(storage):
    for index, scenario in enumerate(
        ("malformed", "unsupported_claim", "persistent_failure")
    ):
        workflow = PurchaseWorkflow(storage, MockModelAdapter(scenario))

        result = workflow.submit(
            make_request(
                request_id=f"PR-B{index}",
                quantity=20,
                unit_price=800,
            )
        )

        assert result["state"] == "BLOCKED", scenario
        assert "insufficient_budget" in result["deterministic_findings"]


def test_blocked_request_with_bad_model_cannot_be_reviewed(storage):
    workflow = PurchaseWorkflow(storage, MockModelAdapter("malformed"))
    workflow.submit(make_request(quantity=20, unit_price=800))

    with pytest.raises(ValueError):
        workflow.review("PR-001", "Rahul", "approve", "Try anyway")


# ---------------------------------------------------------------------------
# Real interruption after tool results were saved
# ---------------------------------------------------------------------------


def test_crash_after_tools_then_restart_does_not_repeat_tool_calls(
    db_path, monkeypatch
):
    calls = _count_tool_calls(monkeypatch)

    crashing = PurchaseWorkflow(
        Storage(db_path), MockModelAdapter("crash_after_tools")
    )
    with pytest.raises(SimulatedCrash):
        crashing.submit(make_request())

    saved = Storage(db_path).get_request("PR-001")
    assert saved["state"] == "RECEIVED"
    assert saved["vendor_result"] is not None
    assert saved["budget_result"] is not None
    assert calls == {"vendor": 1, "budget": 1}

    restarted = PurchaseWorkflow(Storage(db_path), MockModelAdapter("valid"))
    result = restarted.submit(make_request())

    assert result["state"] == "PENDING_REVIEW"
    assert calls == {"vendor": 1, "budget": 1}  # nothing was looked up again


# ---------------------------------------------------------------------------
# Repeated submission
# ---------------------------------------------------------------------------


def test_repeated_submission_keeps_one_final_record(storage):
    workflow = PurchaseWorkflow(storage, MockModelAdapter("valid"))

    workflow.submit(make_request())
    workflow.review("PR-001", "Rahul", "approve", "Verified")

    workflow.submit(make_request())
    result = workflow.submit(make_request())

    duplicates = [
        event
        for event in result["events"]
        if event["event_type"] == "DUPLICATE_SUBMISSION"
    ]
    assert len(duplicates) == 2
    assert storage.final_record_count("PR-001") == 1
    assert result["state"] == "APPROVED"

    refused = workflow.submit(make_request(quantity=11))
    assert refused["errors"] == [
        "request_id_already_exists_with_changed_content"
    ]
    assert refused["state"] == "APPROVED"

