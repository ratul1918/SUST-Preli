"""
QueueStorm Investigator API – FastAPI Application.

Exposes:
  GET  /health          → Health check
  POST /analyze-ticket  → Ticket analysis endpoint
"""

import logging
import traceback

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.investigator import analyze_ticket
from app.schemas import TicketRequest, TicketResponse

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("queuestorm")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="QueueStorm Investigator API",
    description=(
        "AI-powered copilot backend for digital finance support agents. "
        "Analyzes customer complaints, cross-references transaction history, "
        "and generates safe, structured responses."
    ),
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# Custom exception handlers
# ---------------------------------------------------------------------------

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Return a clean 400 error for malformed requests without leaking internals."""
    errors = []
    for error in exc.errors():
        errors.append({
            "field": " → ".join(str(loc) for loc in error.get("loc", [])),
            "message": error.get("msg", "Invalid value"),
            "type": error.get("type", "unknown"),
        })
    return JSONResponse(
        status_code=400,
        content={
            "error": "Invalid request payload",
            "details": errors,
        },
    )


@app.exception_handler(ValidationError)
async def pydantic_validation_handler(
    request: Request,
    exc: ValidationError,
) -> JSONResponse:
    """Handle Pydantic validation errors from response construction."""
    logger.error("Pydantic validation error: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal processing error. Please try again."},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """
    Catch-all exception handler.

    Logs the full traceback internally but returns a safe generic
    message to the client without leaking stack traces or secrets.
    """
    logger.error(
        "Unhandled exception: %s\n%s",
        str(exc),
        traceback.format_exc(),
    )
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error. Our team has been notified."},
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check() -> dict:
    """
    Health readiness endpoint.

    Returns {"status": "ok"} to confirm the service is running.
    """
    return {"status": "ok"}


@app.post("/analyze-ticket", response_model=TicketResponse)
async def analyze_ticket_endpoint(request: TicketRequest) -> TicketResponse:
    """
    Analyze a customer support ticket.

    Accepts a ticket payload with complaint text and optional
    transaction history. Returns structured analysis including
    case classification, evidence verdict, severity, department
    routing, agent summary, and a safe customer reply.
    """
    logger.info(
        "Received ticket %s | language=%s | channel=%s | txn_count=%d",
        request.ticket_id,
        request.language,
        request.channel or "unknown",
        len(request.transaction_history) if request.transaction_history else 0,
    )

    response = await analyze_ticket(request)

    logger.info(
        "Completed ticket %s | case=%s | severity=%s | dept=%s | human_review=%s",
        response.ticket_id,
        response.case_type.value,
        response.severity.value,
        response.department.value,
        response.human_review_required,
    )

    return response


# ---------------------------------------------------------------------------
# Uvicorn entry point (for direct python execution)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    from app.config import settings

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=False,
    )
