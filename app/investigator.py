"""
Investigator orchestration module.

Coordinates the full analysis pipeline:
  Pre-processing rules → LLM query → Post-processing safety → Response

Falls back to the deterministic rule engine when the LLM is
unavailable, times out, or returns unusable output.
"""

import logging
from typing import Any

from app.ai import query_llm
from app.rules import (
    apply_safety_filters,
    determine_department,
    determine_severity,
    generate_safe_fallback,
    guess_case_type,
    match_transaction,
)
from app.schemas import (
    CaseType,
    Department,
    EvidenceVerdict,
    Severity,
    TicketRequest,
    TicketResponse,
)

logger = logging.getLogger(__name__)


async def analyze_ticket(request: TicketRequest) -> TicketResponse:
    """
    Run the full investigation pipeline on a support ticket.

    Pipeline stages:
    1. Pre-processing: transaction matching & keyword hints
    2. LLM query (with timeout + fallback)
    3. Post-processing: fintech safety filters
    4. Response assembly
    """

    # ------------------------------------------------------------------
    # Stage 1: Pre-processing rules
    # ------------------------------------------------------------------
    history = request.transaction_history or []
    txn_id, verdict, reason_codes = match_transaction(request.complaint, history)
    case_hint = guess_case_type(request.complaint)

    # Build pre-analysis context for the LLM
    pre_analysis: dict[str, Any] = {
        "matched_transaction_id": txn_id,
        "evidence_verdict_hint": verdict.value,
        "case_type_hint": case_hint.value if case_hint else "unknown",
        "reason_codes": reason_codes,
    }

    # ------------------------------------------------------------------
    # Stage 2: LLM query (with automatic fallback)
    # ------------------------------------------------------------------
    llm_result = await query_llm(request, pre_analysis)

    if llm_result is None:
        # LLM unavailable or failed → use deterministic fallback
        logger.info(
            "Using rule-based fallback for ticket %s", request.ticket_id
        )
        return generate_safe_fallback(request)

    # ------------------------------------------------------------------
    # Stage 3: Merge LLM output with rule-engine evidence
    # ------------------------------------------------------------------
    # Prefer the rule engine's transaction match if LLM didn't find one
    final_txn_id = llm_result.get("relevant_transaction_id") or txn_id

    # Merge reason codes
    llm_reason_codes = llm_result.get("reason_codes", [])
    merged_reason_codes = list(set(reason_codes + llm_reason_codes))

    # Use the LLM's verdict unless rule engine found an inconsistency
    llm_verdict_str = llm_result.get("evidence_verdict", "insufficient_data")
    if verdict == EvidenceVerdict.inconsistent:
        final_verdict = EvidenceVerdict.inconsistent
        if "rule_engine_inconsistency" not in merged_reason_codes:
            merged_reason_codes.append("rule_engine_inconsistency")
    else:
        try:
            final_verdict = EvidenceVerdict(llm_verdict_str)
        except ValueError:
            final_verdict = verdict

    # Case type: trust LLM but validate
    try:
        final_case_type = CaseType(llm_result.get("case_type", "other"))
    except ValueError:
        final_case_type = case_hint or CaseType.other

    # Department: re-derive from case type for consistency
    try:
        llm_department = Department(llm_result.get("department", "customer_support"))
        final_department = llm_department
    except ValueError:
        final_department = determine_department(final_case_type)

    # Severity
    try:
        final_severity = Severity(llm_result.get("severity", "medium"))
    except ValueError:
        matched_amount = None
        if final_txn_id and history:
            for txn in history:
                if txn.transaction_id == final_txn_id:
                    matched_amount = txn.amount
                    break
        final_severity = determine_severity(final_case_type, matched_amount)

    # Text fields from LLM
    agent_summary = llm_result.get("agent_summary", "Pending review.")
    recommended_action = llm_result.get("recommended_next_action", "Escalate for manual review.")
    customer_reply = llm_result.get("customer_reply", "Thank you for reaching out. We are looking into your concern.")
    human_review = llm_result.get("human_review_required", True)
    confidence = llm_result.get("confidence")

    # ------------------------------------------------------------------
    # Stage 4: Post-processing safety filters
    # ------------------------------------------------------------------
    customer_reply, recommended_action, agent_summary, safety_result = (
        apply_safety_filters(customer_reply, recommended_action, agent_summary)
    )

    if safety_result.has_violations:
        human_review = True
        merged_reason_codes.extend(safety_result.violations)
        if confidence is not None:
            confidence = min(confidence, 0.5)  # reduce confidence
        logger.warning(
            "Safety violations detected for ticket %s: %s",
            request.ticket_id,
            safety_result.violations,
        )

    # Force human review for certain conditions based on rule-based engine logic
    rule_human_review = True
    if final_case_type == CaseType.refund_request and final_verdict == EvidenceVerdict.consistent:
        rule_human_review = False
    elif final_case_type == CaseType.payment_failed and final_verdict == EvidenceVerdict.consistent:
        rule_human_review = False
    elif final_case_type == CaseType.merchant_settlement_delay and final_verdict == EvidenceVerdict.consistent:
        rule_human_review = False
    elif final_case_type == CaseType.other and final_verdict == EvidenceVerdict.insufficient_data:
        rule_human_review = False

    if rule_human_review:
        human_review = True

    # Also force human review for any high/critical severity inconsistent/insufficient_data cases
    if final_verdict in (EvidenceVerdict.inconsistent, EvidenceVerdict.insufficient_data):
        if final_severity in (Severity.high, Severity.critical):
            human_review = True

    # ------------------------------------------------------------------
    # Stage 5: Assemble final response
    # ------------------------------------------------------------------
    return TicketResponse(
        ticket_id=request.ticket_id,
        relevant_transaction_id=final_txn_id,
        evidence_verdict=final_verdict,
        case_type=final_case_type,
        severity=final_severity,
        department=final_department,
        agent_summary=agent_summary,
        recommended_next_action=recommended_action,
        customer_reply=customer_reply,
        human_review_required=human_review,
        confidence=confidence,
        reason_codes=merged_reason_codes if merged_reason_codes else None,
    )
