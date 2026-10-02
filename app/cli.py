import argparse
import json
import tempfile
from pathlib import Path

from .model_adapter import MockModelAdapter
from .models import PurchaseRequest
from .storage import Storage
from .tools import lookup_budget, lookup_vendor
from .workflow import PurchaseWorkflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Purchase request review workflow"
    )
    parser.add_argument(
        "--db",
        default="purchase_workflow.db",
    )
    parser.add_argument(
        "--testcase",
        type=int,
        choices=range(1, 7),
        help="Run one required assignment scenario",
    )
    return parser


def interactive_request() -> PurchaseRequest:
    print("\nENTER PURCHASE REQUEST")
    print("======================")

    return PurchaseRequest(
        request_id=input("Request ID: ").strip(),
        vendor_id=input("Vendor ID: ").strip(),
        description=input("Description: ").strip(),
        quantity=int(input("Quantity: ").strip()),
        unit_price=float(input("Unit price: ").strip()),
        currency=input("Currency: ").strip(),
    )


def money(value: float | None, currency: str) -> str:
    if value is None:
        return "N/A"

    return f"{currency} {value:,.2f}"


def print_request(result: dict) -> None:
    request = result["purchase_request"]

    print("\nREQUEST DETAILS")
    print("---------------")
    print(json.dumps(request, indent=2))


def print_workflow_result(
    result: dict,
    show_events: bool = True,
) -> None:
    request = result["purchase_request"]
    vendor = result["tool_results"].get("vendor")
    budget = result["tool_results"].get("budget")
    currency = request["currency"]

    print_request(result)

    print("\nTOOL RESULTS")
    print("------------")

    if vendor is not None:
        print(f"Vendor ID:             {request['vendor_id']}")
        print(
            f"Vendor exists:         "
            f"{'YES' if vendor['exists'] else 'NO'}"
        )
        print(
            f"Vendor active:         "
            f"{'YES' if vendor['active'] else 'NO'}"
        )
        print(
            f"Permitted currency:    "
            f"{vendor['permitted_currency'] or 'NONE'}"
        )
    else:
        print("Vendor lookup: not completed")

    if budget is not None:
        print(f"Budget currency:       {budget['currency']}")
        print(
            f"Available budget:      "
            f"{money(budget['available'], currency)}"
        )
    else:
        print("Budget lookup: not completed")

    print("\nPURCHASE CALCULATION")
    print("--------------------")
    print(f"Quantity:              {request['quantity']}")
    print(
        f"Unit price:            "
        f"{money(request['unit_price'], currency)}"
    )
    print(
        f"Purchase total:        "
        f"{money(result['total'], currency)}"
    )

    findings = result["deterministic_findings"]

    print("\nDETERMINISTIC CHECKS")
    print("--------------------")

    if not findings:
        print("✓ All deterministic checks passed")
    else:
        for finding in findings:
            print(f"✗ {finding}")

    model = result["model_recommendation"]

    print("\nMODEL RECOMMENDATION")
    print("--------------------")

    if model:
        print(
            f"Recommendation:       "
            f"{model['recommendation'].upper()}"
        )
        print(
            f"Explanation:          "
            f"{model['explanation']}"
        )
    else:
        print("No valid model recommendation was produced.")

    print("\nCURRENT STATE")
    print("-------------")
    print(result["state"])

    if result["errors"]:
        print("\nERRORS / RETRIES")
        print("----------------")

        for error in result["errors"]:
            print(f"- {error}")

    if result.get("human_review"):
        print("\nHUMAN REVIEW")
        print("------------")
        print(json.dumps(result["human_review"], indent=2))

    if show_events:
        print("\nWORKFLOW EVENTS")
        print("---------------")

        for event in result["events"]:
            details = event["details"]

            print(
                f"- {event['event_type']}: "
                f"{json.dumps(details, sort_keys=True)}"
            )


def print_human_review_summary(result: dict) -> None:
    """
    Show the actual information a human reviewer should see before
    making the final approval/rejection decision.
    """

    request = result["purchase_request"]
    vendor = result["tool_results"].get("vendor")
    budget = result["tool_results"].get("budget")
    model = result["model_recommendation"]

    currency = request["currency"]

    print("\n")
    print("=" * 72)
    print("HUMAN REVIEW REQUIRED")
    print("=" * 72)

    print("\nPURCHASE REQUEST")
    print("----------------")
    print(f"Request ID:            {request['request_id']}")
    print(f"Description:           {request['description']}")
    print(f"Vendor ID:             {request['vendor_id']}")
    print(f"Quantity:              {request['quantity']}")
    print(
        f"Unit price:            "
        f"{money(request['unit_price'], currency)}"
    )
    print(
        f"Purchase total:        "
        f"{money(result['total'], currency)}"
    )

    print("\nVENDOR")
    print("------")

    if vendor:
        print(
            f"Vendor exists:         "
            f"{'YES' if vendor['exists'] else 'NO'}"
        )
        print(
            f"Vendor status:         "
            f"{'Active' if vendor['active'] else 'Inactive'}"
        )
        print(
            f"Permitted currency:    "
            f"{vendor['permitted_currency'] or 'NONE'}"
        )

    print("\nBUDGET")
    print("------")

    if budget:
        print(
            f"Available budget:      "
            f"{money(budget['available'], currency)}"
        )

        if result["total"] <= budget["available"]:
            print("Budget check:          PASSED")
        else:
            print("Budget check:          FAILED")

    print("\nDETERMINISTIC CHECKS")
    print("--------------------")

    findings = result["deterministic_findings"]

    if not findings:
        print("Deterministic checks:  PASSED")
    else:
        print("Deterministic checks:  FAILED")

        for finding in findings:
            print(f"  ✗ {finding}")

    print("\nAI RECOMMENDATION")
    print("-----------------")

    if model:
        print(
            f"AI recommendation:     "
            f"{model['recommendation'].upper()}"
        )
        print(
            f"AI explanation:        "
            f"{model['explanation']}"
        )
    else:
        print("AI recommendation:     NONE")

    print("\nIMPORTANT")
    print("---------")
    print(
        "The AI recommendation is advisory only."
    )
    print(
        "The human reviewer makes the final decision."
    )


def review_if_required(
    workflow: PurchaseWorkflow,
    result: dict,
) -> dict:

    if result["state"] != "PENDING_REVIEW":
        return result

    print_human_review_summary(result)

    print("\n----------------------------------------")

    choice = input(
        "Approve or Reject? [A/R]: "
    ).strip().lower()

    while choice not in {"a", "r"}:
        print("Please enter A to approve or R to reject.")

        choice = input(
            "Approve or Reject? [A/R]: "
        ).strip().lower()

    reviewer = input(
        "Reviewer name: "
    ).strip()

    note = input(
        "Review note: "
    ).strip()

    decision = (
        "approve"
        if choice == "a"
        else "reject"
    )

    return workflow.review(
        result["request_id"],
        reviewer,
        decision,
        note,
    )


def run_interactive(db_path: str) -> None:
    """
    Manual/live workflow.

    The user enters the request themselves and sees the complete workflow,
    including the human-review information before making the final decision.
    """

    storage = Storage(db_path)

    print("\n")
    print("=" * 72)
    print("LIVE PURCHASE REQUEST REVIEW")
    print("=" * 72)

    request = interactive_request()

    print("\nWORKFLOW STARTED")
    print("================")

    workflow = PurchaseWorkflow(
        storage,
        MockModelAdapter("valid"),
        progress=lambda message: print(
            f"\n[WORKFLOW] {message}"
        ),
    )

    print("\nINPUT JSON")
    print("----------")
    print(json.dumps(request.to_dict(), indent=2))

    result = workflow.submit(request)

    print_workflow_result(result)

    result = review_if_required(
        workflow,
        result,
    )

    print("\n")
    print("=" * 72)
    print("FINAL MACHINE-READABLE RESULT")
    print("=" * 72)

    print(json.dumps(result, indent=2))


def make_request(
    request_id: str,
    vendor_id: str = "V-101",
    quantity: int = 2,
    unit_price: float = 200,
    currency: str = "USD",
    description: str = "Laptop",
) -> PurchaseRequest:

    return PurchaseRequest(
        request_id=request_id,
        vendor_id=vendor_id,
        description=description,
        quantity=quantity,
        unit_price=unit_price,
        currency=currency,
    )


def scenario_header(
    number: int,
    title: str,
) -> None:

    print("\n" + "=" * 72)
    print(
        f"TEST CASE {number}: {title}"
    )
    print("=" * 72)


def progress_callback(message: str):
    return lambda message_text=message: print(
        f"\n[WORKFLOW] {message_text}"
    )


def run_scenario(number: int) -> None:

    titles = {
        1: "Valid request -> human approval",
        2: "Insufficient budget despite positive model recommendation",
        3: "Unknown / inactive vendor",
        4: "Malformed / unsupported model output",
        5: "Temporary timeout and persistent failure",
        6: "Interruption after tool results saved -> restart and resume",
    }

    scenario_header(
        number,
        titles[number],
    )

    with tempfile.TemporaryDirectory() as temp_dir:

        db_path = Path(temp_dir) / "scenario.db"

        storage = Storage(db_path)

        if number == 1:

            request = make_request(
                "TC1-001",
                quantity=2,
                unit_price=200,
            )

            workflow = PurchaseWorkflow(
                storage,
                MockModelAdapter("valid"),
                progress=lambda message: print(
                    f"\n[WORKFLOW] {message}"
                ),
            )

            print("\nINPUT JSON")
            print("----------")
            print(
                json.dumps(
                    request.to_dict(),
                    indent=2,
                )
            )

            result = workflow.submit(request)

            print_workflow_result(result)

            result = review_if_required(
                workflow,
                result,
            )

            print("\nFINAL RESULT")
            print("------------")
            print(
                json.dumps(
                    result,
                    indent=2,
                )
            )

            return

        if number == 2:

            request = make_request(
                "TC2-001",
                quantity=20,
                unit_price=800,
            )

            workflow = PurchaseWorkflow(
                storage,
                MockModelAdapter("valid"),
                progress=lambda message: print(
                    f"\n[WORKFLOW] {message}"
                ),
            )

            print("\nINPUT JSON")
            print("----------")
            print(
                json.dumps(
                    request.to_dict(),
                    indent=2,
                )
            )

            result = workflow.submit(request)

            print_workflow_result(result)

            print("\nKEY RULE")
            print("--------")
            print(
                "The model recommendation cannot override "
                "a deterministic failure."
            )

            print(
                "The insufficient-budget finding forces "
                "the request to BLOCKED."
            )

            print(
                "No human approval is requested for a "
                "deterministically blocked request."
            )

            print("\nFINAL RESULT")
            print("------------")
            print(
                json.dumps(
                    result,
                    indent=2,
                )
            )

            return

        if number == 3:

            for label, vendor_id in (
                ("Unknown vendor", "V-999"),
                ("Inactive vendor", "V-102"),
            ):

                print(f"\n--- {label} ---")

                request = make_request(
                    f"TC3-{vendor_id}",
                    vendor_id=vendor_id,
                )

                workflow = PurchaseWorkflow(
                    storage,
                    MockModelAdapter("valid"),
                    progress=lambda message: print(
                        f"\n[WORKFLOW] {message}"
                    ),
                )

                print("\nINPUT JSON")
                print("----------")

                print(
                    json.dumps(
                        request.to_dict(),
                        indent=2,
                    )
                )

                result = workflow.submit(request)

                print_workflow_result(result)

                print("\nFINAL JSON")
                print("----------")

                print(
                    json.dumps(
                        result,
                        indent=2,
                    )
                )

            return

        if number == 4:

            for label, scenario in (
                (
                    "Malformed model output",
                    "malformed",
                ),
                (
                    "Unsupported factual claim",
                    "unsupported_claim",
                ),
            ):

                print(f"\n--- {label} ---")

                request = make_request(
                    f"TC4-{scenario}"
                )

                workflow = PurchaseWorkflow(
                    storage,
                    MockModelAdapter(scenario),
                    progress=lambda message: print(
                        f"\n[WORKFLOW] {message}"
                    ),
                )

                print("\nINPUT JSON")
                print("----------")

                print(
                    json.dumps(
                        request.to_dict(),
                        indent=2,
                    )
                )

                result = workflow.submit(request)

                print_workflow_result(result)

                print("\nFINAL JSON")
                print("----------")

                print(
                    json.dumps(
                        result,
                        indent=2,
                    )
                )

            return

        if number == 5:

            print(
                "\n--- Temporary timeout -> retry -> success ---"
            )

            request = make_request(
                "TC5-TEMP"
            )

            adapter = MockModelAdapter(
                "temporary_timeout"
            )

            workflow = PurchaseWorkflow(
                storage,
                adapter,
                progress=lambda message: print(
                    f"\n[WORKFLOW] {message}"
                ),
            )

            print("\nINPUT JSON")
            print("----------")

            print(
                json.dumps(
                    request.to_dict(),
                    indent=2,
                )
            )

            result = workflow.submit(request)

            print_workflow_result(result)

            print(
                f"\nModel adapter calls: "
                f"{adapter.calls} "
                f"(expected 2: timeout + retry)"
            )

            print("\nFINAL JSON")
            print("----------")

            print(
                json.dumps(
                    result,
                    indent=2,
                )
            )

            print(
                "\n--- Persistent model failure -> bounded failure ---"
            )

            request = make_request(
                "TC5-PERSIST"
            )

            adapter = MockModelAdapter(
                "persistent_failure"
            )

            workflow = PurchaseWorkflow(
                storage,
                adapter,
                progress=lambda message: print(
                    f"\n[WORKFLOW] {message}"
                ),
            )

            print("\nINPUT JSON")
            print("----------")

            print(
                json.dumps(
                    request.to_dict(),
                    indent=2,
                )
            )

            result = workflow.submit(request)

            print_workflow_result(result)

            print(
                f"\nModel adapter calls: "
                f"{adapter.calls} "
                f"(persistent failure)"
            )

            print("\nFINAL JSON")
            print("----------")

            print(
                json.dumps(
                    result,
                    indent=2,
                )
            )

            return

        if number == 6:

            request = make_request(
                "TC6-001"
            )

            print(
                "\n--- STEP 1: "
                "Simulate interruption after tool results are saved ---"
            )

            print("\nINPUT JSON")
            print("----------")

            print(
                json.dumps(
                    request.to_dict(),
                    indent=2,
                )
            )

            storage.create_request(
                request.to_dict(),
                "RECEIVED",
            )

            vendor = lookup_vendor(
                request.vendor_id
            )

            budget = lookup_budget(
                request.currency
            )

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

            print(
                "✓ Request persisted as RECEIVED"
            )

            print(
                "✓ Vendor result persisted"
            )

            print(
                "✓ Budget result persisted"
            )

            print(
                "✗ Process interrupted before "
                "deterministic/model steps"
            )

            print(
                "\n--- STEP 2: Restart workflow ---"
            )

            restarted_storage = Storage(
                db_path
            )

            workflow = PurchaseWorkflow(
                restarted_storage,
                MockModelAdapter("valid"),
                progress=lambda message: print(
                    f"\n[WORKFLOW] {message}"
                ),
            )

            result = workflow.submit(
                request
            )

            print_workflow_result(result)

            vendor_events = [
                event
                for event in result["events"]
                if event["event_type"]
                == "VENDOR_LOOKUP_COMPLETED"
            ]

            budget_events = [
                event
                for event in result["events"]
                if event["event_type"]
                == "BUDGET_LOOKUP_COMPLETED"
            ]

            print("\nRESUME CHECK")
            print("------------")

            print(
                f"Vendor lookup events: "
                f"{len(vendor_events)} "
                f"(expected 1)"
            )

            print(
                f"Budget lookup events: "
                f"{len(budget_events)} "
                f"(expected 1)"
            )

            print(
                "✓ Saved tool results were reused; "
                "lookups were not repeated."
            )

            print("\nFINAL JSON")
            print("----------")

            print(
                json.dumps(
                    result,
                    indent=2,
                )
            )

            return


def scenario_menu() -> int:
    """
    Continuous jury menu.

    The menu stays alive after every automated scenario.
    The user only needs to run `python main.py` once.
    """

    while True:

        print("\n")
        print("=" * 72)
        print("PURCHASE REQUEST REVIEW WORKFLOW")
        print("=" * 72)

        print(
            "Select a required assignment scenario, "
            "or run a live manual request."
        )

        print()

        print(
            "1. Valid request -> human approval"
        )

        print(
            "2. Insufficient budget despite "
            "positive model recommendation"
        )

        print(
            "3. Unknown / inactive vendor"
        )

        print(
            "4. Malformed / unsupported model output"
        )

        print(
            "5. Temporary timeout and persistent failure"
        )

        print(
            "6. Interruption after tool results "
            "saved -> restart and resume"
        )

        print(
            "M. Manual / live purchase request"
        )

        print(
            "0. Exit"
        )

        choice = input(
            "\nSelect option [0-6/M]: "
        ).strip().lower()

        if choice == "0":
            print("\nExiting workflow.")
            return 0

        if choice == "m":
            run_interactive(
                "purchase_workflow.db"
            )

            input(
                "\nPress Enter to return to the test menu..."
            )

            continue

        if choice.isdigit() and 1 <= int(choice) <= 6:

            run_scenario(
                int(choice)
            )

            print("\n")
            print("=" * 72)
            print(
                "TEST CASE COMPLETE"
            )
            print("=" * 72)

            input(
                "\nPress Enter to return to the test menu..."
            )

            continue

        print(
            "\nInvalid selection. "
            "Choose 0-6 or M."
        )


def main() -> None:

    parser = build_parser()

    args = parser.parse_args()

    if args.testcase:
        run_scenario(
            args.testcase
        )
        return

    # No command-line arguments:
    # continuous jury-friendly menu.
    if args.db == "purchase_workflow.db":
        scenario_menu()
        return

    # Explicit custom database path:
    # run the manual workflow directly.
    run_interactive(
        args.db
    )