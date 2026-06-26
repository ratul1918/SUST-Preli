"""
Integration test suite for the QueueStorm Investigator API.

Tests cover:
  - Health check endpoint
  - Request validation (400 on missing fields)
  - Full ticket analysis pipeline
  - Safety filter enforcement (credential blocking, promise rewriting)
  - Fallback responses (when LLM is unavailable)
  - Schema compliance
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.rules import apply_safety_filters, generate_safe_fallback, match_transaction
from app.schemas import (
    CaseType,
    EvidenceVerdict,
    TicketRequest,
    TransactionHistoryEntry,
)


client = TestClient(app)


# ---------------------------------------------------------------------------
# Health check tests
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    """Tests for GET /health."""

    def test_health_returns_200(self):
        response = client.get("/health")
        assert response.status_code == 200

    def test_health_returns_ok_status(self):
        response = client.get("/health")
        data = response.json()
        assert data == {"status": "ok"}


# ---------------------------------------------------------------------------
# Request validation tests
# ---------------------------------------------------------------------------

class TestRequestValidation:
    """Tests for POST /analyze-ticket input validation."""

    def test_missing_ticket_id(self):
        response = client.post("/analyze-ticket", json={
            "complaint": "My money is missing",
        })
        assert response.status_code == 400

    def test_missing_complaint(self):
        response = client.post("/analyze-ticket", json={
            "ticket_id": "TKT-001",
        })
        assert response.status_code == 400

    def test_empty_body(self):
        response = client.post("/analyze-ticket", json={})
        assert response.status_code == 400

    def test_invalid_json(self):
        response = client.post(
            "/analyze-ticket",
            content="not valid json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code in (400, 422)

    def test_invalid_transaction_type_enum(self):
        response = client.post("/analyze-ticket", json={
            "ticket_id": "TKT-001",
            "complaint": "Test complaint",
            "transaction_history": [{
                "transaction_id": "TXN-001",
                "timestamp": "2026-04-14T14:08:22Z",
                "type": "invalid_type",
                "amount": 1000,
                "counterparty": "+8801700000000",
                "status": "completed",
            }],
        })
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Full pipeline tests (uses rule-based fallback since no API key in tests)
# ---------------------------------------------------------------------------

class TestAnalyzeTicket:
    """Tests for the full POST /analyze-ticket pipeline."""

    def test_basic_ticket_returns_200(self):
        response = client.post("/analyze-ticket", json={
            "ticket_id": "TKT-TEST-001",
            "complaint": "I sent 5000 taka to a wrong number",
            "transaction_history": [{
                "transaction_id": "TXN-9101",
                "timestamp": "2026-04-14T14:08:22Z",
                "type": "transfer",
                "amount": 5000,
                "counterparty": "+8801719876543",
                "status": "completed",
            }],
        })
        assert response.status_code == 200

    def test_response_echoes_ticket_id(self):
        response = client.post("/analyze-ticket", json={
            "ticket_id": "TKT-ECHO-TEST",
            "complaint": "Payment issue",
        })
        data = response.json()
        assert data["ticket_id"] == "TKT-ECHO-TEST"

    def test_response_has_all_required_fields(self):
        response = client.post("/analyze-ticket", json={
            "ticket_id": "TKT-SCHEMA-001",
            "complaint": "My payment failed but money was deducted",
        })
        data = response.json()
        required_fields = [
            "ticket_id",
            "relevant_transaction_id",
            "evidence_verdict",
            "case_type",
            "severity",
            "department",
            "agent_summary",
            "recommended_next_action",
            "customer_reply",
            "human_review_required",
        ]
        for field in required_fields:
            assert field in data, f"Missing required field: {field}"

    def test_evidence_verdict_is_valid_enum(self):
        response = client.post("/analyze-ticket", json={
            "ticket_id": "TKT-ENUM-001",
            "complaint": "Refund request for duplicate payment",
        })
        data = response.json()
        valid_verdicts = ["consistent", "inconsistent", "insufficient_data"]
        assert data["evidence_verdict"] in valid_verdicts

    def test_case_type_is_valid_enum(self):
        response = client.post("/analyze-ticket", json={
            "ticket_id": "TKT-ENUM-002",
            "complaint": "I got a phishing call asking for OTP",
        })
        data = response.json()
        valid_case_types = [
            "wrong_transfer", "payment_failed", "refund_request",
            "duplicate_payment", "merchant_settlement_delay",
            "agent_cash_in_issue", "phishing_or_social_engineering", "other",
        ]
        assert data["case_type"] in valid_case_types

    def test_severity_is_valid_enum(self):
        response = client.post("/analyze-ticket", json={
            "ticket_id": "TKT-ENUM-003",
            "complaint": "Payment failed",
        })
        data = response.json()
        assert data["severity"] in ["low", "medium", "high", "critical"]

    def test_department_is_valid_enum(self):
        response = client.post("/analyze-ticket", json={
            "ticket_id": "TKT-ENUM-004",
            "complaint": "Settlement delay from merchant",
        })
        data = response.json()
        valid_depts = [
            "customer_support", "dispute_resolution", "payments_ops",
            "merchant_operations", "agent_operations", "fraud_risk",
        ]
        assert data["department"] in valid_depts

    def test_no_transaction_history_returns_insufficient_data(self):
        response = client.post("/analyze-ticket", json={
            "ticket_id": "TKT-NOHIST-001",
            "complaint": "Something went wrong with my payment",
        })
        data = response.json()
        assert data["evidence_verdict"] == "insufficient_data"

    def test_human_review_is_boolean(self):
        response = client.post("/analyze-ticket", json={
            "ticket_id": "TKT-BOOL-001",
            "complaint": "Refund needed",
        })
        data = response.json()
        assert isinstance(data["human_review_required"], bool)


# ---------------------------------------------------------------------------
# Transaction matching unit tests
# ---------------------------------------------------------------------------

class TestTransactionMatching:
    """Tests for the rule engine's transaction matching logic."""

    def _make_txn(self, **overrides) -> TransactionHistoryEntry:
        defaults = {
            "transaction_id": "TXN-001",
            "timestamp": "2026-04-14T14:08:22Z",
            "type": "transfer",
            "amount": 5000,
            "counterparty": "+8801719876543",
            "status": "completed",
        }
        defaults.update(overrides)
        return TransactionHistoryEntry(**defaults)

    def test_match_by_transaction_id(self):
        txn = self._make_txn(transaction_id="TXN-9101")
        txn_id, verdict, codes = match_transaction(
            "Please check TXN-9101", [txn]
        )
        assert txn_id == "TXN-9101"
        assert verdict == EvidenceVerdict.consistent

    def test_match_by_amount(self):
        txn = self._make_txn(amount=5000)
        txn_id, verdict, codes = match_transaction(
            "I sent 5000 taka but it didn't arrive", [txn]
        )
        assert txn_id is not None
        assert "amount_match" in codes or "weak_amount_match" in codes

    def test_no_match_returns_insufficient(self):
        txn = self._make_txn(amount=1000, transaction_id="TXN-999")
        txn_id, verdict, codes = match_transaction(
            "Something happened with 5000 taka TXN-123", [txn]
        )
        # No ID or amount match, so should be insufficient
        assert verdict == EvidenceVerdict.insufficient_data

    def test_empty_history(self):
        txn_id, verdict, codes = match_transaction(
            "My payment failed", []
        )
        assert txn_id is None
        assert verdict == EvidenceVerdict.insufficient_data

    def test_none_history(self):
        txn_id, verdict, codes = match_transaction(
            "My payment failed", None
        )
        assert txn_id is None
        assert verdict == EvidenceVerdict.insufficient_data


# ---------------------------------------------------------------------------
# Safety filter unit tests
# ---------------------------------------------------------------------------

class TestSafetyFilters:
    """Tests for post-processing safety filters."""

    def test_credential_leak_blocked(self):
        reply = "Please share your OTP and PIN to proceed with the refund."
        safe_reply, _, _, result = apply_safety_filters(reply, "Action", "Summary")
        assert "OTP" not in safe_reply.upper() or "never share" in safe_reply.lower()
        assert result.force_human_review is True
        assert result.has_violations

    def test_refund_promise_rewritten(self):
        reply = "We will refund your money immediately."
        safe_reply, _, _, result = apply_safety_filters(reply, "Action", "Summary")
        assert "official channels" in safe_reply.lower()
        assert result.has_violations

    def test_safe_reply_passes_through(self):
        reply = "Thank you for reaching out. We are investigating your concern."
        safe_reply, _, _, result = apply_safety_filters(reply, "Check details", "Customer complaint")
        assert safe_reply == reply
        assert not result.has_violations

    def test_third_party_contact_blocked(self):
        reply = "Please call this number 01712345678 for help."
        safe_reply, _, _, result = apply_safety_filters(reply, "Action", "Summary")
        assert result.has_violations or "official" in safe_reply.lower()


# ---------------------------------------------------------------------------
# Fallback response tests
# ---------------------------------------------------------------------------

class TestFallbackGenerator:
    """Tests for the deterministic fallback response generator."""

    def test_fallback_returns_valid_response(self):
        request = TicketRequest(
            ticket_id="TKT-FALLBACK-001",
            complaint="My payment failed",
        )
        response = generate_safe_fallback(request)
        assert response.ticket_id == "TKT-FALLBACK-001"
        assert response.human_review_required is True
        assert response.evidence_verdict == EvidenceVerdict.insufficient_data

    def test_fallback_with_transaction_history(self):
        request = TicketRequest(
            ticket_id="TKT-FALLBACK-002",
            complaint="I sent 5000 taka to wrong number via TXN-9101",
            transaction_history=[
                TransactionHistoryEntry(
                    transaction_id="TXN-9101",
                    timestamp="2026-04-14T14:08:22Z",
                    type="transfer",
                    amount=5000,
                    counterparty="+8801719876543",
                    status="completed",
                ),
            ],
        )
        response = generate_safe_fallback(request)
        assert response.relevant_transaction_id == "TXN-9101"
        assert response.case_type == CaseType.wrong_transfer

    def test_fallback_never_asks_for_credentials(self):
        request = TicketRequest(
            ticket_id="TKT-FALLBACK-003",
            complaint="Please give me my OTP",
        )
        response = generate_safe_fallback(request)
        reply_lower = response.customer_reply.lower()
        assert "otp" not in reply_lower or "do not share" in reply_lower or "never share" in reply_lower

    def test_fallback_has_reason_code(self):
        request = TicketRequest(
            ticket_id="TKT-FALLBACK-004",
            complaint="Generic complaint",
        )
        response = generate_safe_fallback(request)
        assert response.reason_codes is not None
        assert "fallback_response" in response.reason_codes
