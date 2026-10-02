import json
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
FIXTURES_DIR = BASE_DIR / "fixtures"


def _load_json(filename: str) -> Any:
    with open(FIXTURES_DIR / filename, "r", encoding="utf-8") as file:
        return json.load(file)


def lookup_vendor(vendor_id: str) -> dict[str, Any]:
    vendors = _load_json("vendors.json")
    vendor = vendors.get(vendor_id)

    if vendor is None:
        return {
            "exists": False,
            "active": False,
            "permitted_currency": None,
        }

    return {
        "exists": True,
        "active": bool(vendor["active"]),
        "permitted_currency": vendor["permitted_currency"],
    }


def lookup_budget(currency: str) -> dict[str, Any]:
    budgets = _load_json("budget.json")
    return {
        "currency": currency,
        "available": float(budgets.get(currency, 0)),
    }
