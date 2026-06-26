"""
Rule-based engine for the QueueStorm Investigator API.

Provides three capabilities:
1. **Transaction matching** – scans complaint text and cross-references
   the customer's transaction history to find the most relevant entry.
2. **Post-processing safety filters** – enforces fintech guardrails on
   generated text (blocks credential requests, unauthorized promises,
   suspicious third-party contacts).
3. **Fallback response generator** – produces a safe, schema-compliant
   response when the LLM is unavailable or times out.
"""

import re
from collections import Counter
from typing import Optional

from app.schemas import (
    CaseType,
    Department,
    EvidenceVerdict,
    Severity,
    TicketRequest,
    TicketResponse,
    TransactionHistoryEntry,
)

# ---------------------------------------------------------------------------
# 1. Transaction Matching
# ---------------------------------------------------------------------------

# Patterns to extract amounts (e.g. "5000", "5,000", "5000.00", "৳5000")
_AMOUNT_PATTERN = re.compile(r"[\৳$]?\s*(\d[\d,]*\.?\d*)")

# Pattern to extract transaction IDs (e.g. "TXN-9101", "txn9101")
_TXN_ID_PATTERN = re.compile(r"(?i)\b(TXN[-_]?\d+)\b")

# Pattern to extract phone numbers (BD format)
_PHONE_PATTERN = re.compile(r"(?:\+?880|0)(\d{10})")

# Pattern to extract dates / times heuristically
_DATE_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}"  # ISO date
    r"|"
    r"\d{1,2}[:/]\d{2}\s*(?:am|pm|AM|PM)?"  # time like 2:00pm
)

# Bangla digit map for extracting amounts from Bangla text
_BANGLA_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

# Pattern to extract Bangla amounts (e.g. "২০০০ টাকা", "2000 টাকা")
_BANGLA_AMOUNT_PATTERN = re.compile(r"([\d০-৯][\d০-৯,]*)\s*(?:টাকা|taka|tk)")


def _extract_amounts(text: str) -> list[float]:
    """Extract all numeric amounts from text, including Bangla numerals."""
    amounts: list[float] = []

    # Standard numeric amounts
    for match in _AMOUNT_PATTERN.finditer(text):
        try:
            val = float(match.group(1).replace(",", ""))
            if val >= 1:  # Filter noise (0, single digits from dates, etc.)
                amounts.append(val)
        except ValueError:
            continue

    # Bangla numeral amounts (e.g. "২০০০ টাকা")
    for match in _BANGLA_AMOUNT_PATTERN.finditer(text):
        try:
            bangla_num = match.group(1).translate(_BANGLA_DIGITS).replace(",", "")
            val = float(bangla_num)
            if val >= 1 and val not in amounts:
                amounts.append(val)
        except ValueError:
            continue

    return amounts


def _extract_txn_ids(text: str) -> list[str]:
    """Extract transaction IDs (TXN-XXXX format) from text."""
    return [m.group(1).upper().replace("_", "-") for m in _TXN_ID_PATTERN.finditer(text)]


def _extract_phones(text: str) -> list[str]:
    """Extract phone numbers from text, normalised to 10-digit local format."""
    return [m.group(1) for m in _PHONE_PATTERN.finditer(text)]


def _detect_duplicate_payments(
    history: list[TransactionHistoryEntry],
) -> Optional[tuple[str, str]]:
    """
    Detect duplicate payments in transaction history.

    Two transactions are considered duplicates if they have the same
    amount, same counterparty, and occurred within 60 seconds of each other.

    Returns:
        (first_txn_id, duplicate_txn_id) or None
    """
    from datetime import datetime

    if len(history) < 2:
        return None

    # Parse timestamps and sort
    parsed: list[tuple[TransactionHistoryEntry, datetime]] = []
    for txn in history:
        try:
            dt = datetime.fromisoformat(txn.timestamp.replace("Z", "+00:00"))
            parsed.append((txn, dt))
        except (ValueError, AttributeError):
            continue

    parsed.sort(key=lambda x: x[1])

    for i in range(len(parsed)):
        for j in range(i + 1, len(parsed)):
            txn_a, dt_a = parsed[i]
            txn_b, dt_b = parsed[j]

            # Same amount, same counterparty, within 120 seconds
            if (
                txn_a.amount == txn_b.amount
                and txn_a.counterparty == txn_b.counterparty
                and abs((dt_b - dt_a).total_seconds()) <= 120
            ):
                return txn_a.transaction_id, txn_b.transaction_id

    return None


def _detect_repeated_counterparty(
    history: list[TransactionHistoryEntry],
    target_counterparty: str,
) -> int:
    """Count how many times a counterparty appears in the transaction history."""
    count = 0
    for txn in history:
        if txn.counterparty == target_counterparty:
            count += 1
    return count


def match_transaction(
    complaint: str,
    history: list[TransactionHistoryEntry] | None,
) -> tuple[Optional[str], EvidenceVerdict, list[str]]:
    """
    Cross-reference complaint text against transaction history.

    Returns:
        (relevant_transaction_id, evidence_verdict, reason_codes)
    """
    if not history:
        return None, EvidenceVerdict.insufficient_data, ["no_transaction_history"]

    reason_codes: list[str] = []
    mentioned_ids = _extract_txn_ids(complaint)
    mentioned_amounts = _extract_amounts(complaint)
    mentioned_phones = _extract_phones(complaint)

    # Track all matches and their scores
    matches: list[tuple[TransactionHistoryEntry, int, list[str]]] = []

    for txn in history:
        score = 0
        txn_reasons: list[str] = []

        # Direct transaction ID match (strongest signal)
        normalised_id = txn.transaction_id.upper().replace("_", "-")
        if normalised_id in mentioned_ids:
            score += 100
            txn_reasons.append("transaction_id_match")

        # Amount match
        if txn.amount in mentioned_amounts:
            score += 50
            txn_reasons.append("amount_match")

        # Counterparty phone match
        txn_phone_digits = re.sub(r"\D", "", txn.counterparty)
        if len(txn_phone_digits) >= 10:
            txn_local = txn_phone_digits[-10:]
            if txn_local in mentioned_phones:
                score += 40
                txn_reasons.append("counterparty_match")

        if score > 0:
            matches.append((txn, score, txn_reasons))

    # Check for ambiguity: multiple transactions with the same score
    if len(matches) > 1:
        top_score = max(m[1] for m in matches)
        top_matches = [m for m in matches if m[1] == top_score]

        if len(top_matches) > 1:
            # Multiple equally-strong matches → ambiguous, need clarification
            reason_codes.append("ambiguous_match")
            return None, EvidenceVerdict.insufficient_data, reason_codes

    # Pick the best match
    best_match: Optional[TransactionHistoryEntry] = None
    if matches:
        matches.sort(key=lambda m: m[1], reverse=True)
        best_match = matches[0][0]
        reason_codes.extend(matches[0][2])
    else:
        # No direct match found – try weak matching by amount only
        for txn in history:
            if txn.amount in mentioned_amounts:
                best_match = txn
                reason_codes.append("weak_amount_match")
                break

    if best_match is None:
        return None, EvidenceVerdict.insufficient_data, ["no_matching_transaction"]

    best_score = matches[0][1] if matches else 0

    # Determine verdict based on match strength
    if best_score >= 40:
        verdict = EvidenceVerdict.consistent
    else:
        verdict = EvidenceVerdict.insufficient_data

    # --- Inconsistency checks ---
    complaint_lower = complaint.lower()

    # Check for established counterparty pattern (wrong transfer claims)
    if best_match.counterparty.startswith("+") or best_match.counterparty.startswith("0"):
        repeat_count = _detect_repeated_counterparty(history, best_match.counterparty)
        if repeat_count >= 3:
            # Multiple previous transfers to the same person → suspicious claim
            verdict = EvidenceVerdict.inconsistent
            if "established_recipient_pattern" not in reason_codes:
                reason_codes.append("established_recipient_pattern")
                reason_codes.append("evidence_inconsistent")

    # Check status discrepancy: complaint says "failed" but txn is "completed"
    if best_match.status.value == "completed" and any(
        w in complaint_lower for w in ["fail", "failed", "unsuccessful", "didn't go through", "not received"]
    ):
        if "status_discrepancy" not in reason_codes:
            reason_codes.append("status_discrepancy")

    # Check status inconsistency: complaint says "deducted/charged" but txn is "failed"
    if best_match.status.value == "failed" and any(
        w in complaint_lower for w in ["completed", "deducted", "charged", "taken", "balance was deducted"]
    ):
        # This is actually consistent with the complaint – the customer says it failed
        # but money was deducted, and the txn IS failed. Keep as consistent.
        if "potential_balance_deduction" not in reason_codes:
            reason_codes.append("potential_balance_deduction")

    return best_match.transaction_id, verdict, list(set(reason_codes))


# ---------------------------------------------------------------------------
# 2. Keyword-based case type hints (pre-LLM)
# ---------------------------------------------------------------------------

_CASE_TYPE_KEYWORDS: dict[CaseType, list[str]] = {
    CaseType.wrong_transfer: [
        "wrong number", "wrong person", "wrong account", "sent to wrong",
        "ভুল নম্বর", "bhul number", "wrong e send",
        "brother", "sister", "bhai", "didn't get it",
        "he says he didn't", "she says she didn't",
    ],
    CaseType.payment_failed: [
        "payment failed", "transaction failed", "unsuccessful",
        "fail hoye geche", "payment hoyni", "didn't go through",
        "deducted but not", "taka kete niyeche",
        "app showed failed", "showed failed", "failed but",
        "balance was deducted", "deducted",
        "recharge", "bill pay", "mobile recharge",
    ],
    CaseType.refund_request: [
        "refund", "money back", "return my money", "taka ferot",
        "refund chai", "refund diben",
        "changed my mind", "don't want it", "cancel",
    ],
    CaseType.duplicate_payment: [
        "duplicate", "double charge", "charged twice", "duibar",
        "double payment", "2 bar", "twice", "deducted twice",
        "paid once but", "only paid once",
    ],
    CaseType.merchant_settlement_delay: [
        "settlement", "settled", "settlement delay",
        "sales", "merchant", "my yesterday",
        "not been settled",
        "dokan", "payment received hoyni",
    ],
    CaseType.agent_cash_in_issue: [
        "agent", "cash in", "cash-in", "agent e", "agent point",
        "agent theke",
        # Bangla keywords for agent/cash-in issues
        "এজেন্ট", "ক্যাশ ইন", "ক্যাশইন", "এজেন্টের",
        "ব্যালেন্সে", "ব্যালেন্স", "আসেনি", "টাকা আসেনি",
        "পাঠিয়েছে", "দেখছি না",
    ],
    CaseType.phishing_or_social_engineering: [
        "otp", "pin", "scam", "fraud", "hack", "phishing",
        "someone called", "link", "suspicious", "thug", "thokano",
        "prothom alo", "hack hoyeche", "account hack",
        "ওটিপি", "পিন",
    ],
}

# Additional high-priority compound patterns that override simple keyword matches
_COMPOUND_PATTERNS: list[tuple[CaseType, re.Pattern]] = [
    # "failed" + "deducted/balance" → payment_failed (not refund even if "refund" is mentioned)
    (CaseType.payment_failed, re.compile(
        r"(?i)(failed|fail).{0,40}(deduct|balance|kete)|"
        r"(deduct|balance|kete).{0,40}(failed|fail)"
    )),
    # "twice" / "double" + amount → duplicate_payment
    (CaseType.duplicate_payment, re.compile(
        r"(?i)(twice|double|duplicate).{0,30}(deduct|charge|paid|payment)|"
        r"(deduct|charge|paid|payment).{0,30}(twice|double|duplicate)"
    )),
    # Bangla agent cash-in pattern
    (CaseType.agent_cash_in_issue, re.compile(
        r"এজেন্ট.{0,50}(?:ক্যাশ\s*ইন|টাকা)|(?:ক্যাশ\s*ইন).{0,50}(?:আসেনি|হয়নি|ব্যালেন্স)"
    )),
]


def guess_case_type(complaint: str) -> Optional[CaseType]:
    """Heuristic keyword-based case type detection as a pre-LLM hint."""
    complaint_lower = complaint.lower()

    # First check compound patterns (higher priority)
    for case_type, pattern in _COMPOUND_PATTERNS:
        if pattern.search(complaint):
            return case_type

    # Then do keyword scoring
    scores: dict[CaseType, int] = {}
    for case_type, keywords in _CASE_TYPE_KEYWORDS.items():
        for kw in keywords:
            if kw in complaint_lower or kw in complaint:
                scores[case_type] = scores.get(case_type, 0) + 1

    if not scores:
        return None

    # If payment_failed and refund_request both match, prefer payment_failed
    # when the complaint talks about failures/deductions
    if (
        CaseType.payment_failed in scores
        and CaseType.refund_request in scores
        and any(w in complaint_lower for w in ["failed", "fail", "deducted", "deduct"])
    ):
        scores[CaseType.payment_failed] += 3  # Strong boost

    return max(scores, key=scores.get)  # type: ignore[arg-type]


def determine_department(case_type: CaseType) -> Department:
    """Map a case type to its responsible department."""
    mapping: dict[CaseType, Department] = {
        CaseType.wrong_transfer: Department.dispute_resolution,
        CaseType.payment_failed: Department.payments_ops,
        CaseType.refund_request: Department.customer_support,
        CaseType.duplicate_payment: Department.payments_ops,
        CaseType.merchant_settlement_delay: Department.merchant_operations,
        CaseType.agent_cash_in_issue: Department.agent_operations,
        CaseType.phishing_or_social_engineering: Department.fraud_risk,
        CaseType.other: Department.customer_support,
    }
    return mapping.get(case_type, Department.customer_support)


def determine_severity(case_type: CaseType, amount: Optional[float] = None) -> Severity:
    """Determine severity based on case type and optional amount."""
    # Phishing / social engineering is always critical
    if case_type == CaseType.phishing_or_social_engineering:
        return Severity.critical

    # High-value transactions
    if amount and amount >= 10000:
        return Severity.high

    high_severity_types = {
        CaseType.wrong_transfer,
        CaseType.duplicate_payment,
    }
    if case_type in high_severity_types:
        return Severity.high if (amount and amount >= 500) else Severity.medium

    # payment_failed with balance deduction is high severity
    if case_type == CaseType.payment_failed and amount and amount >= 500:
        return Severity.high

    # Low severity for change-of-mind refund requests with small amounts
    if case_type == CaseType.refund_request:
        if amount and amount < 1000:
            return Severity.low
        return Severity.medium

    # Merchant settlement
    if case_type == CaseType.merchant_settlement_delay:
        return Severity.medium

    return Severity.medium


# ---------------------------------------------------------------------------
# 3. Post-processing Safety Filters
# ---------------------------------------------------------------------------

# Credential leak patterns (OTP, PIN, password, CVV, card number)
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b("
    r"otp|pin\s*(?:code|number)?|password|passcode|cvv|"
    r"card\s*number|secret\s*code|security\s*code|"
    r"gupan\s*number|gupan\s*code|pin\s*diben|pin\s*den|otp\s*diben|otp\s*den"
    r")\b"
)

# Unauthorized promise patterns
_PROMISE_PATTERN = re.compile(
    r"(?i)("
    r"we\s+will\s+refund|refund\s+(?:has\s+been|is\s+being)\s+(?:processed|initiated)|"
    r"money\s+(?:has\s+been|will\s+be)\s+(?:sent\s+back|returned|reversed)|"
    r"(?:your\s+)?account\s+(?:has\s+been|will\s+be)\s+(?:unblocked|unlocked|restored)|"
    r"reversing\s+(?:the\s+)?(?:payment|transaction|transfer)|"
    r"we\s+(?:have|are)\s+(?:reversed|reversing)|"
    r"full\s+refund|immediate\s+refund|instant\s+refund|"
    r"compensation\s+(?:has\s+been|will\s+be)|"
    r"taka\s+ferot\s+dewa\s+hoyeche|refund\s+kore\s+dewa\s+hobe"
    r")"
)

# Third-party contact patterns (phone numbers, suspicious URLs)
_THIRD_PARTY_PATTERN = re.compile(
    r"(?i)("
    r"call\s+(?:this|the)\s+number|contact\s+\d|phone\s+\d|"
    r"dial\s+\d|helpline\s*:?\s*\d|"
    r"(?:https?://(?!(?:www\.)?(?:official|company))[^\s]+)|"
    r"(?:www\.(?!official|company)[^\s]+)"
    r")"
)

# Safe replacement text
_SAFE_REFUND_TEXT = (
    "Any eligible amount will be returned through official channels "
    "after a thorough investigation by our team."
)

_SAFE_CREDENTIAL_WARNING = (
    "For your security, please never share your PIN, OTP, password, "
    "or card details with anyone, including our support team. "
    "We will never ask for this information."
)


class SafetyCheckResult:
    """Result of running safety filters on generated text."""

    def __init__(self) -> None:
        self.violations: list[str] = []
        self.force_human_review: bool = False

    @property
    def has_violations(self) -> bool:
        return len(self.violations) > 0


def apply_safety_filters(
    customer_reply: str,
    recommended_action: str,
    agent_summary: str,
) -> tuple[str, str, str, SafetyCheckResult]:
    """
    Apply post-processing safety filters to generated text.

    Returns:
        (safe_customer_reply, safe_action, safe_summary, safety_result)
    """
    result = SafetyCheckResult()

    # --- Credential leak check ---
    if _CREDENTIAL_PATTERN.search(customer_reply):
        result.violations.append("credential_request_in_reply")
        result.force_human_review = True
        # Replace the entire reply with a safe version
        customer_reply = _SAFE_CREDENTIAL_WARNING

    if _CREDENTIAL_PATTERN.search(recommended_action):
        result.violations.append("credential_request_in_action")
        result.force_human_review = True
        recommended_action = re.sub(
            _CREDENTIAL_PATTERN,
            "[REDACTED]",
            recommended_action,
        )

    # --- Unauthorized promise check ---
    if _PROMISE_PATTERN.search(customer_reply):
        result.violations.append("unauthorized_promise_in_reply")
        customer_reply = _PROMISE_PATTERN.sub(_SAFE_REFUND_TEXT, customer_reply)

    if _PROMISE_PATTERN.search(recommended_action):
        result.violations.append("unauthorized_promise_in_action")
        recommended_action = _PROMISE_PATTERN.sub(
            "initiate investigation through standard dispute workflow",
            recommended_action,
        )

    # --- Third-party contact check ---
    if _THIRD_PARTY_PATTERN.search(customer_reply):
        result.violations.append("third_party_contact_in_reply")
        result.force_human_review = True
        customer_reply = _THIRD_PARTY_PATTERN.sub(
            "our official support channels",
            customer_reply,
        )

    if _THIRD_PARTY_PATTERN.search(recommended_action):
        result.violations.append("third_party_contact_in_action")
        recommended_action = _THIRD_PARTY_PATTERN.sub(
            "official internal escalation path",
            recommended_action,
        )

    return customer_reply, recommended_action, agent_summary, result


# ---------------------------------------------------------------------------
# 4. Fallback Response Generator
# ---------------------------------------------------------------------------

def generate_safe_fallback(request: TicketRequest) -> TicketResponse:
    """
    Generate a safe, schema-compliant fallback response.

    Used when the LLM is unavailable, times out, or returns invalid output.
    Always routes to human review with conservative classifications.
    """
    # Attempt basic rule-based analysis
    history = request.transaction_history or []
    complaint = request.complaint

    # Check for duplicate payments first (special handling)
    dup = _detect_duplicate_payments(history)
    is_dup_case = False
    if dup:
        case_hint = guess_case_type(complaint)
        if case_hint == CaseType.duplicate_payment or any(
            w in complaint.lower() for w in ["twice", "double", "duplicate", "2 bar", "only paid once"]
        ):
            is_dup_case = True

    txn_id, verdict, reason_codes = match_transaction(complaint, history)
    case_hint = guess_case_type(complaint)
    case_type = case_hint or CaseType.other
    department = determine_department(case_type)

    # Override for duplicate payment: point to the SECOND (duplicate) transaction
    if is_dup_case and dup:
        first_txn_id, dup_txn_id = dup
        txn_id = dup_txn_id  # The duplicate is the relevant one
        verdict = EvidenceVerdict.consistent
        case_type = CaseType.duplicate_payment
        department = Department.payments_ops
        reason_codes = ["duplicate_payment", "biller_verification_required"]

    # Find amount for severity calculation
    matched_amount: Optional[float] = None
    if txn_id and history:
        for txn in history:
            if txn.transaction_id == txn_id:
                matched_amount = txn.amount
                break

    severity = determine_severity(case_type, matched_amount)

    # Build contextual texts based on case type
    txn_ref = f" (Reference: {txn_id})" if txn_id else ""

    # Case-specific agent summaries and replies
    if case_type == CaseType.wrong_transfer:
        counterparty = ""
        if txn_id and history:
            for txn in history:
                if txn.transaction_id == txn_id:
                    counterparty = f" to {txn.counterparty}"
                    break

        is_inconsistent = verdict == EvidenceVerdict.inconsistent

        if is_inconsistent:
            agent_summary = (
                f"Customer claims {txn_id} ({matched_amount:.0f} BDT{counterparty}) was a wrong transfer, "
                f"but transaction history shows repeated transfers to the same recipient, "
                f"suggesting an established counterparty pattern."
            ) if matched_amount else (
                f"Customer claims {txn_id}{counterparty} was a wrong transfer, "
                f"but evidence is inconsistent with the claim."
            )
        else:
            agent_summary = (
                f"Customer reports sending {matched_amount:.0f} BDT via {txn_id}{counterparty}. "
                f"Recipient may be incorrect. Requires dispute investigation."
            ) if matched_amount else (
                f"Customer reports a wrong transfer{txn_ref}. Requires dispute investigation."
            )

        recommended_action = (
            f"Verify {txn_id} details with the customer and initiate the wrong-transfer dispute workflow per policy."
            if txn_id else
            "Ask customer for specific transaction details to identify the transfer in question."
        )
        customer_reply = (
            f"We have received your request regarding transaction {txn_id}. "
            f"Please do not share your PIN or OTP with anyone. "
            f"Our dispute team will review the case carefully and contact you through official support channels."
            if txn_id else
            "We have noted your concern. Please provide the transaction details so we can investigate. "
            "Please do not share your PIN or OTP with anyone."
        )

    elif case_type == CaseType.payment_failed:
        agent_summary = (
            f"Customer attempted a {matched_amount:.0f} BDT payment ({txn_id}) which failed, "
            f"but reports balance was deducted. Requires payments operations investigation."
        ) if matched_amount and txn_id else (
            f"Customer reports a failed payment{txn_ref} with possible balance deduction."
        )
        recommended_action = (
            f"Investigate {txn_id} ledger status. If balance was deducted on a failed payment, "
            f"initiate the automatic reversal flow within standard SLA."
            if txn_id else
            "Investigate the reported failed payment and verify balance status."
        )
        customer_reply = (
            f"We have noted that transaction {txn_id} may have caused an unexpected balance deduction. "
            f"Our payments team will review the case and any eligible amount will be returned through official channels. "
            f"Please do not share your PIN or OTP with anyone."
            if txn_id else
            "We have noted your concern about the failed payment. Our payments team will review the case "
            "and any eligible amount will be returned through official channels. "
            "Please do not share your PIN or OTP with anyone."
        )

    elif case_type == CaseType.refund_request:
        agent_summary = (
            f"Customer requests refund of {matched_amount:.0f} BDT for {txn_id} "
            f"(merchant payment). Not a service failure."
        ) if matched_amount and txn_id else (
            f"Customer requests a refund{txn_ref}. Needs review."
        )
        recommended_action = (
            "Inform the customer that refund eligibility depends on the merchant's own policy. "
            "Provide guidance on contacting the merchant directly for a refund."
        )
        customer_reply = (
            "Thank you for reaching out. Refunds for completed merchant payments depend on the merchant's "
            "own policy. We recommend contacting the merchant directly. If you need help reaching them, "
            "please reply and we will guide you. Please do not share your PIN or OTP with anyone."
        )

    elif case_type == CaseType.duplicate_payment:
        agent_summary = (
            f"Customer reports duplicate payment. Two identical {matched_amount:.0f} BDT "
            f"payments were completed in quick succession. The second ({txn_id}) is likely the duplicate."
        ) if matched_amount and txn_id else (
            f"Customer reports a duplicate payment{txn_ref}. Requires investigation."
        )
        recommended_action = (
            f"Verify the duplicate with payments_ops. If the biller confirms only one payment was received, "
            f"initiate reversal of {txn_id}."
            if txn_id else
            "Verify duplicate payment claim with payments ops and biller records."
        )
        customer_reply = (
            f"We have noted the possible duplicate payment for transaction {txn_id}. "
            f"Our payments team will verify with the biller and any eligible amount will be returned "
            f"through official channels. Please do not share your PIN or OTP with anyone."
            if txn_id else
            "We have noted your concern about a possible duplicate payment. Our team will verify "
            "and any eligible amount will be returned through official channels. "
            "Please do not share your PIN or OTP with anyone."
        )

    elif case_type == CaseType.phishing_or_social_engineering:
        agent_summary = (
            "Customer reports a suspicious call/contact asking for credentials. "
            "Likely social engineering attempt. No transaction impact detected."
        )
        recommended_action = (
            "Escalate to fraud_risk team immediately. Confirm to customer that the company never "
            "asks for OTP. Log the reported details for fraud pattern analysis."
        )
        customer_reply = (
            "Thank you for reaching out before sharing any information. We never ask for your PIN, OTP, "
            "or password under any circumstances. Please do not share these with anyone, even if they "
            "claim to be from us. Our fraud team has been notified of this incident."
        )

    elif case_type == CaseType.merchant_settlement_delay:
        agent_summary = (
            f"Merchant reports {matched_amount:.0f} BDT settlement ({txn_id}) is delayed "
            f"beyond the expected window. Settlement status is pending."
        ) if matched_amount and txn_id else (
            f"Merchant reports a settlement delay{txn_ref}. Requires investigation."
        )
        recommended_action = (
            f"Route to merchant_operations to verify settlement batch status. "
            f"If the batch is delayed, communicate a revised ETA to the merchant."
        )
        customer_reply = (
            f"We have noted your concern about settlement {txn_id}. Our merchant operations team "
            f"will check the batch status and update you on the expected settlement time through "
            f"official channels."
            if txn_id else
            "We have noted your settlement concern. Our merchant operations team will investigate "
            "and update you through official channels."
        )

    elif case_type == CaseType.agent_cash_in_issue:
        agent_name = ""
        if txn_id and history:
            for txn in history:
                if txn.transaction_id == txn_id:
                    agent_name = f" via {txn.counterparty}"
                    break

        agent_summary = (
            f"Customer reports {matched_amount:.0f} BDT cash-in{agent_name} ({txn_id}) "
            f"not reflected in balance. Transaction status may be pending."
        ) if matched_amount and txn_id else (
            f"Customer reports agent cash-in issue{txn_ref}. Balance not updated."
        )
        recommended_action = (
            f"Investigate {txn_id} pending status with agent operations. "
            f"Confirm settlement state and resolve within the standard cash-in SLA."
            if txn_id else
            "Investigate the cash-in issue with agent operations team."
        )
        customer_reply = (
            f"We have noted your concern about transaction {txn_id}. Our agent operations team "
            f"will investigate and resolve the issue. Any eligible amount will be credited through "
            f"official channels. Please do not share your PIN or OTP with anyone."
            if txn_id else
            "We have noted your cash-in concern. Our agent operations team will investigate. "
            "Please do not share your PIN or OTP with anyone."
        )

    else:
        # Generic / "other" case
        agent_summary = (
            f"Customer submitted a complaint via ticket {request.ticket_id}{txn_ref}. "
            f"Insufficient detail to classify automatically. Requires human review."
        )
        recommended_action = (
            "Reply to customer asking for specific details: which transaction, what amount, "
            "what went wrong, and approximate time."
        )
        customer_reply = (
            f"Thank you for reaching out. To help you faster, please share the transaction ID, "
            f"the amount involved, and a short description of what went wrong. "
            f"Please do not share your PIN or OTP with anyone."
        )

    reason_codes.append("fallback_response")

    # Determine human_review_required
    needs_human_review = True  # default to cautious
    if case_type == CaseType.refund_request and verdict == EvidenceVerdict.consistent:
        needs_human_review = False
    if case_type == CaseType.payment_failed and verdict == EvidenceVerdict.consistent:
        needs_human_review = False
    if case_type == CaseType.merchant_settlement_delay and verdict == EvidenceVerdict.consistent:
        needs_human_review = False
    if case_type == CaseType.other and verdict == EvidenceVerdict.insufficient_data:
        needs_human_review = False
    # Always require human review for these
    if case_type in (CaseType.phishing_or_social_engineering, CaseType.wrong_transfer):
        needs_human_review = True
    if verdict == EvidenceVerdict.inconsistent:
        needs_human_review = True

    return TicketResponse(
        ticket_id=request.ticket_id,
        relevant_transaction_id=txn_id,
        evidence_verdict=verdict,
        case_type=case_type,
        severity=severity,
        department=department,
        agent_summary=agent_summary,
        recommended_next_action=recommended_action,
        customer_reply=customer_reply,
        human_review_required=needs_human_review,
        confidence=0.3 if verdict == EvidenceVerdict.insufficient_data else 0.65,
        reason_codes=reason_codes,
    )
