# QueueStorm Investigator API - Development To-Do List

This is the checklist of implementation tasks to build the QueueStorm Investigator API. 

## [ ] Phase 1: Project Setup and Structure
- [ ] Initialize Python environment (virtual environment recommended).
- [ ] Create folder structure:
  - `app/` (for application source code)
  - `tests/` (for unit and integration tests)
- [ ] Create `requirements.txt` with dependencies:
  - `fastapi`
  - `uvicorn`
  - `pydantic`
  - `google-genai` (or `google-generativeai` / `openai` depending on selected provider)
  - `python-dotenv`
  - `pytest`
  - `httpx` (for testing)
- [ ] Create `.env.example` with standard variable placeholders:
  - `PORT=8000`
  - `GEMINI_API_KEY=`
  - `MODEL_NAME=gemini-2.5-flash`
- [ ] Create `Dockerfile` using lightweight `python:3.10-slim`.
- [ ] Implement initial `app/config.py` to load environment variables safely.

## [ ] Phase 2: Schema & API Contract Implementation
- [ ] Implement `app/schemas.py` using Pydantic:
  - [ ] Define `TransactionHistoryEntry` schema with fields:
    - `transaction_id`: string
    - `timestamp`: string (ISO 8601)
    - `type`: string enum (transfer, payment, cash_in, cash_out, settlement, refund)
    - `amount`: number
    - `counterparty`: string
    - `status`: string enum (completed, failed, pending, reversed)
  - [ ] Define `TicketRequest` schema:
    - `ticket_id`: string (Required)
    - `complaint`: string (Required)
    - `language`: string (Optional, default="en")
    - `channel`: string (Optional)
    - `user_type`: string (Optional)
    - `campaign_context`: string (Optional)
    - `transaction_history`: list of `TransactionHistoryEntry` (Optional)
    - `metadata`: dict (Optional)
  - [ ] Define `TicketResponse` schema with exact field names, types, and required enums:
    - `ticket_id`: string (Must echo request)
    - `relevant_transaction_id`: string or null
    - `evidence_verdict`: enum (`consistent`, `inconsistent`, `insufficient_data`)
    - `case_type`: enum (`wrong_transfer`, `payment_failed`, `refund_request`, `duplicate_payment`, `merchant_settlement_delay`, `agent_cash_in_issue`, `phishing_or_social_engineering`, `other`)
    - `severity`: enum (`low`, `medium`, `high`, `critical`)
    - `department`: enum (`customer_support`, `dispute_resolution`, `payments_ops`, `merchant_operations`, `agent_operations`, `fraud_risk`)
    - `agent_summary`: string (1-2 sentences)
    - `recommended_next_action`: string
    - `customer_reply`: string
    - `human_review_required`: boolean
    - `confidence`: float (Optional)
    - `reason_codes`: list of strings (Optional)
- [ ] Implement `app/main.py` endpoints:
  - [ ] `GET /health` responding with `{"status": "ok"}`. Must respond in under 60 seconds (target < 1 second).
  - [ ] `POST /analyze-ticket` receiving `TicketRequest`.
  - [ ] Set up custom FastAPI exception handlers to:
    - Return `HTTP 400` for invalid JSON or Pydantic validation errors.
    - Return `HTTP 500` for internal exceptions without leaking stack traces, secrets, or internal codes.

## [ ] Phase 3: Rule-Based Pre-Processing (The Investigator Twist)
- [ ] Implement `app/rules.py` containing transaction matching logic:
  - [ ] Extract currency amounts, transaction IDs, phone numbers, or dates mentioned in the complaint text.
  - [ ] Match extracted data against entries in `transaction_history`.
  - [ ] Handle edge cases:
    - Empty or missing `transaction_history` -> set `relevant_transaction_id = null` and `evidence_verdict = "insufficient_data"`.
    - Single matching transaction -> verify details.
    - Multiple matching transactions -> resolve by date proximity or amount, or mark as ambiguous/flag for human review.
  - [ ] Implement basic classification rules for obvious cases (e.g. if text contains "PIN" or "OTP" requests, classify as `phishing_or_social_engineering`).

## [ ] Phase 4: Hybrid LLM/AI Integration
- [ ] Implement `app/ai.py` to communicate with the LLM API (Gemini/OpenAI):
  - [ ] Create system prompts instructing the model on the task, required taxonomy, and output format.
  - [ ] Implement prompt injection defense (prevent adversarial complaints from overriding system guidelines).
  - [ ] Configure the LLM call to output structured JSON matching the Pydantic schemas.
  - [ ] Add explicit instructions to handle Bangla/Banglish text (translating or extracting meaning while preserving context).
- [ ] Implement `app/investigator.py` to orchestrate:
  - [ ] Run transaction matching rules.
  - [ ] Prepare context and query LLM.
  - [ ] Parse LLM output into `TicketResponse`.

## [ ] Phase 5: Fintech Safety Guardrails (Post-Processing)
- [ ] Implement hard safety constraints in `app/rules.py` to run on LLM outputs before returning them:
  - [ ] **Credential Rule**: Scan `customer_reply` for any request for PIN, OTP, password, or full card numbers. If found, remove them, replace with a security warning, and force `human_review_required = True`. (Avoids -15 point penalty)
  - [ ] **Refund Promise Rule**: Scan `customer_reply` and `recommended_next_action` for unauthorized promises of refund, reversal, account unblock, or recovery. Replace with safe neutral wording: *"any eligible amount will be returned through official channels"*. (Avoids -10 point penalty)
  - [ ] **Third-Party Support Rule**: Scan `customer_reply` for phone numbers or external link instructions. Ensure they only point to official channels. Remove suspicious third-party contacts. (Avoids -10 point penalty)
  - [ ] Force `human_review_required = true` if:
    - Verdict is `inconsistent` or `insufficient_data` for a critical request.
    - Case type is `phishing_or_social_engineering`.
    - Safety guardrails were triggered.

## [ ] Phase 6: Fallback and Stability Mechanisms
- [ ] Implement timeout and error fallback logic:
  - [ ] Measure time remaining for the request (POST timeout is 30s; target response is < 5s).
  - [ ] Add a `timeout` parameter of 15 seconds to the LLM API call.
  - [ ] Catch connection errors, rate limit errors, and timeouts.
  - [ ] If an error occurs, invoke `rules.generate_safe_fallback(...)` which generates a valid response with `evidence_verdict = "insufficient_data"`, safe default replies, and `human_review_required = true`.

## [ ] Phase 7: Local Verification and Testing
- [ ] Write integration tests in `tests/test_api.py`:
  - Test validation error triggers on missing fields.
  - Test health check success.
  - Test that critical safety rules strip out unauthorized refund promises or credential requests.
- [ ] Run test suite locally using `pytest`.
- [ ] Download or load `SUST_Preli_Sample_Cases.json` (if available) and run all 10 sample cases against the local server to verify that the output structure and values match exactly.

## [ ] Phase 8: Docker Packaging & Build Verification
- [ ] Write `Dockerfile` and ensure image size is optimized:
  - Use `.dockerignore` to exclude `.git`, virtual environments, and caching folders.
  - Verify size is under 500MB (limit is 1GB).
- [ ] Build the image locally:
  ```bash
  docker build -t queuestorm-team .
  ```
- [ ] Run the container using:
  ```bash
  docker run -p 8000:8000 --env-file judging.env queuestorm-team
  ```
- [ ] Call `GET http://localhost:8000/health` to confirm it responds within 60s.

## [ ] Phase 9: Documentation & Prep for Submission
- [ ] Finalize `README.md` in workspace containing:
  - Setup and install instructions.
  - Running instructions (both local and Docker).
  - Models used and reasoning.
  - Safety logic explanation.
  - Known limitations.
- [ ] Ensure `.env.example` is complete and does not contain real API keys.
- [ ] Perform a final check of code to ensure no stack traces or API keys are printed or returned in responses.
