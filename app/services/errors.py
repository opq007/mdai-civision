"""OpenAI-style error handling."""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import JSONResponse


def openai_error(
    status: int,
    message: str,
    err_type: str = "invalid_request_error",
    *,
    code: str | None = None,
    param: str | None = None,
) -> HTTPException:
    """Build an HTTPException that renders as an OpenAI error body."""
    return HTTPException(
        status_code=status,
        detail={
            "error": {
                "message": message,
                "type": err_type,
                "code": code,
                "param": param,
            }
        },
    )


def openai_error_response(
    status: int,
    message: str,
    err_type: str = "invalid_request_error",
    *,
    code: str | None = None,
    param: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": err_type,
                "code": code,
                "param": param,
            }
        },
    )
