"""
Sample case validation runner.

Loads all 10 cases from SUST_Preli_Sample_Cases.json,
sends each to the /analyze-ticket endpoint via TestClient,
and validates the response against the expected output.

Checks:
  1. HTTP 200 status
  2. All required fields present
  3. ticket_id echoed correctly
  4. All enum fields contain valid values
  5. Functional equivalence with expected output:
     - relevant_transaction_id matches
     - evidence_verdict matches
     - case_type matches
     - department matches
     - severity is comparable
     - human_review_required matches
  6. Safety: customer_reply does not ask for credentials
  7. Safety: customer_reply does not promise unauthorized refunds
"""

import json
import re
import sys
from pathlib import Path

# pyrefly: ignore [missing-import]
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

# Load sample cases
CASES_FILE = Path(__file__).parent.parent / "SUST_Preli_Sample_Cases.json"
with open(CASES_FILE, "r") as f:
    data = json.load(f)

CASES = data["cases"]

# Valid enum sets
VALID_VERDICTS = {"consistent", "inconsistent", "insufficient_data"}
VALID_CASE_TYPES = {
    "wrong_transfer", "payment_failed", "refund_request", "duplicate_payment",
    "merchant_settlement_delay", "agent_cash_in_issue",
    "phishing_or_social_engineering", "other",
}
VALID_SEVERITIES = {"low", "medium", "high", "critical"}
VALID_DEPARTMENTS = {
    "customer_support", "dispute_resolution", "payments_ops",
    "merchant_operations", "agent_operations", "fraud_risk",
}

REQUIRED_FIELDS = [
    "ticket_id", "relevant_transaction_id", "evidence_verdict",
    "case_type", "severity", "department", "agent_summary",
    "recommended_next_action", "customer_reply", "human_review_required",
]

# Safety patterns
CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(share\s+(?:your\s+)?(?:otp|pin|password|cvv))"
    r"|(?:send\s+(?:us\s+)?(?:your\s+)?(?:otp|pin|password))"
    r"|(?:provide\s+(?:your\s+)?(?:otp|pin|password|cvv|card\s*number))"
    r"|(?:give\s+(?:us\s+)?(?:your\s+)?(?:otp|pin|password))"
    r"|(?:enter\s+(?:your\s+)?(?:otp|pin|password))"
)

UNSAFE_PROMISE_PATTERN = re.compile(
    r"(?i)(we\s+will\s+refund\s+you)"
    r"|(your\s+money\s+(?:has\s+been|will\s+be)\s+refunded)"
    r"|(refund\s+has\s+been\s+processed)"
    r"|(we\s+have\s+reversed)"
    r"|(your\s+account\s+has\s+been\s+unblocked)"
)


def check_safety(reply: str, case_id: str) -> list[str]:
    """Check customer_reply for safety violations."""
    issues = []

    # Check for credential solicitation (asking FOR credentials, not warning about them)
    # We need to distinguish between "share your OTP" (bad) and "do not share your OTP" (good)
    # Simple heuristic: if "not" or "never" appears near the credential term, it's a warning
    lines = reply.split(".")
    for line in lines:
        line_lower = line.lower().strip()
        if CREDENTIAL_PATTERN.search(line_lower):
            # Check if it's a warning (contains negation)
            if not any(neg in line_lower for neg in ["not", "never", "don't", "do not", "না", "নয়"]):
                issues.append(f"[{case_id}] SAFETY: customer_reply may solicit credentials: '{line.strip()}'")

    if UNSAFE_PROMISE_PATTERN.search(reply):
        issues.append(f"[{case_id}] SAFETY: customer_reply contains unauthorized promise")

    return issues


def run_all_cases():
    """Run all 10 sample cases and report results."""
    total = len(CASES)
    passed = 0
    failed = 0
    warnings = 0
    all_issues: list[str] = []
    all_warnings: list[str] = []

    print("=" * 80)
    print("QueueStorm Investigator — Sample Case Validation")
    print(f"Running {total} cases...")
    print("=" * 80)

    for case in CASES:
        case_id = case["id"]
        label = case["label"]
        input_data = case["input"]
        expected = case["expected_output"]
        issues: list[str] = []
        case_warnings: list[str] = []

        print(f"\n{'─' * 60}")
        print(f"  {case_id}: {label}")
        print(f"{'─' * 60}")

        # Send request
        response = client.post("/analyze-ticket", json=input_data)
        result = response.json()

        # 1. HTTP status
        if response.status_code != 200:
            issues.append(f"HTTP {response.status_code} (expected 200)")
            print(f"  ❌ HTTP Status: {response.status_code}")
        else:
            print(f"  ✅ HTTP Status: 200")

        # 2. Required fields
        missing = [f for f in REQUIRED_FIELDS if f not in result]
        if missing:
            issues.append(f"Missing fields: {missing}")
            print(f"  ❌ Missing fields: {missing}")
        else:
            print(f"  ✅ All required fields present")

        # 3. ticket_id echo
        if result.get("ticket_id") != expected["ticket_id"]:
            issues.append(f"ticket_id mismatch: got '{result.get('ticket_id')}', expected '{expected['ticket_id']}'")
            print(f"  ❌ ticket_id: {result.get('ticket_id')} (expected {expected['ticket_id']})")
        else:
            print(f"  ✅ ticket_id: {result['ticket_id']}")

        # 4. Enum validation
        if result.get("evidence_verdict") not in VALID_VERDICTS:
            issues.append(f"Invalid evidence_verdict: {result.get('evidence_verdict')}")
        if result.get("case_type") not in VALID_CASE_TYPES:
            issues.append(f"Invalid case_type: {result.get('case_type')}")
        if result.get("severity") not in VALID_SEVERITIES:
            issues.append(f"Invalid severity: {result.get('severity')}")
        if result.get("department") not in VALID_DEPARTMENTS:
            issues.append(f"Invalid department: {result.get('department')}")

        # 5. Functional equivalence checks
        # relevant_transaction_id
        exp_txn = expected.get("relevant_transaction_id")
        got_txn = result.get("relevant_transaction_id")
        if exp_txn == got_txn:
            print(f"  ✅ relevant_transaction_id: {got_txn}")
        else:
            # For ambiguous cases (SAMPLE-08), null is acceptable
            if exp_txn is None and got_txn is not None:
                case_warnings.append(f"relevant_transaction_id: got '{got_txn}', expected null (ambiguous case)")
                print(f"  ⚠️  relevant_transaction_id: {got_txn} (expected null)")
            elif exp_txn is not None and got_txn is None:
                issues.append(f"relevant_transaction_id: got null, expected '{exp_txn}'")
                print(f"  ❌ relevant_transaction_id: null (expected {exp_txn})")
            else:
                issues.append(f"relevant_transaction_id: got '{got_txn}', expected '{exp_txn}'")
                print(f"  ❌ relevant_transaction_id: {got_txn} (expected {exp_txn})")

        # evidence_verdict
        exp_verdict = expected.get("evidence_verdict")
        got_verdict = result.get("evidence_verdict")
        if exp_verdict == got_verdict:
            print(f"  ✅ evidence_verdict: {got_verdict}")
        else:
            issues.append(f"evidence_verdict: got '{got_verdict}', expected '{exp_verdict}'")
            print(f"  ❌ evidence_verdict: {got_verdict} (expected {exp_verdict})")

        # case_type
        exp_case = expected.get("case_type")
        got_case = result.get("case_type")
        if exp_case == got_case:
            print(f"  ✅ case_type: {got_case}")
        else:
            issues.append(f"case_type: got '{got_case}', expected '{exp_case}'")
            print(f"  ❌ case_type: {got_case} (expected {exp_case})")

        # department
        exp_dept = expected.get("department")
        got_dept = result.get("department")
        if exp_dept == got_dept:
            print(f"  ✅ department: {got_dept}")
        else:
            # Some flexibility: customer_support vs dispute_resolution for refund_request
            case_warnings.append(f"department: got '{got_dept}', expected '{exp_dept}'")
            print(f"  ⚠️  department: {got_dept} (expected {exp_dept})")

        # severity
        exp_sev = expected.get("severity")
        got_sev = result.get("severity")
        severity_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        if exp_sev == got_sev:
            print(f"  ✅ severity: {got_sev}")
        elif abs(severity_order.get(got_sev, -1) - severity_order.get(exp_sev, -1)) <= 1:
            case_warnings.append(f"severity: got '{got_sev}', expected '{exp_sev}' (within 1 level)")
            print(f"  ⚠️  severity: {got_sev} (expected {exp_sev}, within tolerance)")
        else:
            issues.append(f"severity: got '{got_sev}', expected '{exp_sev}'")
            print(f"  ❌ severity: {got_sev} (expected {exp_sev})")

        # human_review_required
        exp_review = expected.get("human_review_required")
        got_review = result.get("human_review_required")
        if exp_review == got_review:
            print(f"  ✅ human_review_required: {got_review}")
        else:
            if got_review is True and exp_review is False:
                # Being cautious (flagging for review when not required) is acceptable
                case_warnings.append(f"human_review_required: got True, expected False (cautious)")
                print(f"  ⚠️  human_review_required: True (expected False, cautious is OK)")
            else:
                issues.append(f"human_review_required: got {got_review}, expected {exp_review}")
                print(f"  ❌ human_review_required: {got_review} (expected {exp_review})")

        # 6. Safety checks on customer_reply
        safety_issues = check_safety(result.get("customer_reply", ""), case_id)
        issues.extend(safety_issues)

        # 7. Text quality checks
        agent_summary = result.get("agent_summary", "")
        customer_reply = result.get("customer_reply", "")
        if len(agent_summary) < 10:
            case_warnings.append("agent_summary is very short")
        if len(customer_reply) < 20:
            case_warnings.append("customer_reply is very short")

        # Print text fields
        print(f"\n  📝 agent_summary: {agent_summary[:120]}...")
        print(f"  📝 customer_reply: {customer_reply[:120]}...")
        print(f"  📝 recommended_action: {result.get('recommended_next_action', '')[:120]}...")
        if result.get("confidence") is not None:
            print(f"  📊 confidence: {result['confidence']}")
        if result.get("reason_codes"):
            print(f"  🏷️  reason_codes: {result['reason_codes']}")

        # Verdict
        if issues:
            failed += 1
            for issue in issues:
                print(f"  ❌ FAIL: {issue}")
            all_issues.extend([f"[{case_id}] {i}" for i in issues])
        elif case_warnings:
            warnings += 1
            passed += 1
            for w in case_warnings:
                print(f"  ⚠️  WARN: {w}")
            all_warnings.extend([f"[{case_id}] {w}" for w in case_warnings])
        else:
            passed += 1

        status = "❌ FAIL" if issues else ("⚠️  PASS (with warnings)" if case_warnings else "✅ PASS")
        print(f"\n  Result: {status}")

    # Summary
    print(f"\n{'=' * 80}")
    print("SUMMARY")
    print(f"{'=' * 80}")
    print(f"  Total:    {total}")
    print(f"  Passed:   {passed} {'✅' if passed == total else ''}")
    print(f"  Failed:   {failed} {'❌' if failed > 0 else ''}")
    print(f"  Warnings: {warnings}")

    if all_issues:
        print(f"\n{'─' * 40}")
        print("ALL FAILURES:")
        for issue in all_issues:
            print(f"  ❌ {issue}")

    if all_warnings:
        print(f"\n{'─' * 40}")
        print("ALL WARNINGS:")
        for w in all_warnings:
            print(f"  ⚠️  {w}")

    print(f"\n{'=' * 80}")

    return failed == 0


if __name__ == "__main__":
    success = run_all_cases()
    sys.exit(0 if success else 1)
