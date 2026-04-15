from __future__ import annotations

from http import HTTPStatus
from typing import cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ExceptionHandler

PROBLEM_JSON_MEDIA_TYPE = "application/problem+json"


def _status_title(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def _problem(
    *,
    request: Request,
    status_code: int,
    title: str,
    detail: str,
) -> dict[str, str | int]:
    return {
        "type": "about:blank",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": request.url.path,
    }


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    title = _status_title(exc.status_code)
    detail = title if exc.detail is None else str(exc.detail)
    problem = _problem(
        request=request,
        status_code=exc.status_code,
        title=title,
        detail=detail,
    )
    return JSONResponse(
        problem,
        status_code=exc.status_code,
        media_type=PROBLEM_JSON_MEDIA_TYPE,
        headers=exc.headers,
    )


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    errors = exc.errors()
    if errors:
        first = errors[0]
        loc = first.get("loc", ())
        msg = first.get("msg", "validation error")
        detail = f"{loc}: {msg}"
    else:
        detail = "validation error"

    status_code = 422
    title = "Unprocessable Entity"
    problem = _problem(
        request=request,
        status_code=status_code,
        title=title,
        detail=detail,
    )
    return JSONResponse(
        problem,
        status_code=status_code,
        media_type=PROBLEM_JSON_MEDIA_TYPE,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(
        HTTPException,
        cast(ExceptionHandler, http_exception_handler),
    )
    app.add_exception_handler(
        RequestValidationError,
        cast(ExceptionHandler, validation_exception_handler),
    )
