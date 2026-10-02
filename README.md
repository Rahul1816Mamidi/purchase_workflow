# Purchase Request Review Workflow

A small Python workflow that reviews purchase requests using **deterministic checks + a model adapter + human approval**, with SQLite persistence and resume.
No UI, API key or external service is needed.

---

## 1. Context

A company buys **gadgets / products** from a fixed list of vendors. Each request is checked before a human decides.

**Currencies supported:** `USD`, `EUR`

| Currency | Available budget |
|---|---|
| USD | 10,000 |
| EUR | 12,000 |

**Available vendors** (`fixtures/vendors.json`):

```json
{
  "V-101": { "active": true,  "permitted_currency": "USD" },
  "V-102": { "active": false, "permitted_currency": "USD" },
  "V-103": { "active": true,  "permitted_currency": "EUR" }
}
```

Any other vendor ID (e.g. `V-999`) is treated as **unknown**.

---

## 2. How to run

**Requirements:** Python 3.10+

```bash
python -m pip install -r requirements.txt   # installs pytest only
python main.py
```

You will see:

```text
PURCHASE REQUEST REVIEW WORKFLOW
Select a required assignment scenario, or run a live manual request.

1. Valid request -> human approval
2. Insufficient budget despite positive model recommendation
3. Unknown / inactive vendor
4. Malformed / unsupported model output
5. Temporary timeout and persistent failure
6. Interruption after tool results saved -> restart and resume
M. Manual / live purchase request
0. Exit

Select option [0-6/M]:
```

| Option | What it does |
|---|---|
| `1`-`6` | Runs a ready-made assignment scenario and prints inputs, tool results, findings, model output, state, errors/retries and events |
| `M` | Manual mode: type your own request in the terminal |

### Manual mode (`M`)

Answer the prompts, for example:

```text
Request ID:  PR-100
Vendor ID:   V-101
Description: 5 gadgets
Quantity:    5
Unit price:  300
Currency:    USD
```

### How to review a request

If the request reaches `PENDING_REVIEW`, the terminal asks:

```text
Approve or Reject? [A/R]:
Reviewer name:
Review note:
```

The reviewer name, decision and note are saved. The final state becomes `APPROVED` or `REJECTED`.
(In code: `PurchaseWorkflow.review(request_id, reviewer, decision, note)`.)

### Run tests

```bash
python -m pytest -q
```

13 tests cover: valid + approval, insufficient budget, unknown/inactive vendor, currency mismatch, malformed and unsupported model output, timeout then success, persistent failure, resume after saved tools, repeated submission, changed content with same ID, and review only from `PENDING_REVIEW`.

---

## 3. Architecture

```text
 Purchase Request (terminal / scenario)
        |
        v
+--------------------------------------------------------------+
| 1. VALIDATE INPUT                     [Python dataclass]     |
|    required fields, qty > 0, price > 0, uppercase currency   |
|    why: facts must never come from the model                 |
+--------------------------------------------------------------+
        | invalid -> BLOCKED
        v
+--------------------------------------------------------------+
| 2. TOOLS (callable functions)         [Python + JSON files]  |
|    lookup_vendor()  -> exists / active / currency            |
|    lookup_budget()  -> available amount                      |
|    result saved to DB right after each call                  |
|    why: simple, no service needed; saved result = no repeat  |
+--------------------------------------------------------------+
        |
        v
+--------------------------------------------------------------+
| 3. DETERMINISTIC CHECKS               [Python]               |
|    total = qty x unit price                                  |
|    vendor_not_found | vendor_inactive | currency_mismatch    |
|    insufficient_budget                                       |
|    why: exact, testable, cannot be "talked around"           |
+--------------------------------------------------------------+
        |
        v
+--------------------------------------------------------------+
| 4. MODEL ADAPTER                      [ModelAdapter interface|
|    returns recommendation + explanation   + MockAdapter]     |
|    retry only on temporary errors (max 2 attempts)           |
|    why: model is advisory; the interface lets us swap models |
+--------------------------------------------------------------+
        |
        v
+--------------------------------------------------------------+
| 5. VALIDATE MODEL OUTPUT              [Python]               |
|    schema check + known-claim checks                         |
|    (model confidence is never trusted)                       |
+--------------------------------------------------------------+
        | bad output / retries exhausted -> FAILED
        v
+--------------------------------------------------------------+
| 6. DECISION GATE                      [Python]               |
|    any finding  -> BLOCKED  (model cannot override)          |
|    no findings  -> PENDING_REVIEW                            |
+--------------------------------------------------------------+
        |
        v
+--------------------------------------------------------------+
| 7. HUMAN REVIEW                       [Terminal prompt]      |
|    reviewer name + decision + note                           |
|    -> APPROVED / REJECTED                                    |
+--------------------------------------------------------------+

 SQLite (single file) <-- every step writes: request, tool results,
                          findings, model result, review, events
```

### States

```text
RECEIVED --> BLOCKED         (deterministic check failed)
         --> FAILED          (model error / invalid model output)
         --> PENDING_REVIEW --> APPROVED
                            --> REJECTED
```

### Why this stack (and not others)

| Choice | Why | Not chosen |
|---|---|---|
| Plain Python functions | Flow is fixed and sequential; easy to read, test and modify | LangGraph / CrewAI: extra complexity for no benefit here |
| SQLite | Zero setup, durable across restarts, queryable, ships with Python | JSON files (no safe updates); Postgres/Redis (overkill for a local exercise) |
| Adapter interface + mock | Runs offline; models can be swapped without touching the workflow | Calling a real API directly (needs keys, not repeatable) |
| JSON fixtures as tools | Meets "no external service" and stays easy to inspect | Real DB / API |
| Event table | Gives a full audit trail and a machine-readable history | Only logging to console |

### Idempotency rules

- Same request ID + same content: existing record is reused, no duplicate final record.
- Same request ID + changed content: rejected with an error and logged as `CONFLICTING_RESUBMISSION`; the original record is not modified.
- Saved vendor/budget/model results are reused after a restart.

---

## 4. Deterministic code vs model

| Deterministic (Python) | Model (adapter) |
|---|---|
| Input validation | Recommendation: `approve` / `reject` |
| Vendor + budget lookup | Short explanation text |
| Total calculation | |
| Inactive vendor, currency mismatch, budget checks | |
| Blocking decision, retry limits | |
| Persistence, resume | |
| Human approval | |

**Unsupported claims we can detect:** invented vendor history ("10 years"), "within budget" when the total exceeds it, and "vendor checks passed" when they did not.
**Not detected:** any other invented fact (e.g. discounts, delivery dates), because the check is rule-based, not full fact verification.

---

## 5. Replacing the mock with a local model (e.g. Ollama)

Only one new class is needed; the workflow code does not change.

```text
Workflow --> ModelAdapter.recommend()
                 |-- MockModelAdapter     (today)
                 '-- OllamaModelAdapter   (local model)
                        POST http://localhost:11434/api/chat
                        -> JSON reply -> ModelRecommendation
```

Steps:

1. Create `OllamaModelAdapter(ModelAdapter)` that sends the request + tool results as a prompt.
2. Ask for JSON-only output (Ollama supports a JSON `format`), then parse it.
3. Map connection timeouts to `TemporaryModelError` and other failures to `PersistentModelError`.
4. Pass the new adapter to `PurchaseWorkflow`. Retries, validation and review stay the same.

**What improves with a real local model:**

- Real explanations instead of canned ones, with data staying on the machine.
- A stricter prompt: "use only the facts provided".
- Stronger claim checks, e.g. every number in the explanation must match the supplied data.

---

## 6. Remaining limitations

- Input is manual. A UI could be added, and real purchase documents (invoices/quotes) could be uploaded with **OCR** to fill the fields automatically.
- Claim validation is rule-based. A production version could use structured model output (JSON schema) with field-level fact checks.
- Vendor and budget data are static JSON files. Production would use real vendor/ERP/finance APIs.
- Production would also need: reviewer login and roles, concurrent-write protection, notifications for pending reviews, and a retry/backoff policy.

---

## 7. Time spent and AI assistance

**Approx. time:** 3.5 hours (pipeline design, implementation, error fixing, git commit).

**AI assistance (OpenAI):**

- Help fixing some errors.
- Help with the logic of the mock model adapter.

All code was reviewed 

---

## 8. Reused vs new code

All code (workflow, storage, tools, mock adapter, CLI, tests) was newly written for this exercise. No external project code was reused.
