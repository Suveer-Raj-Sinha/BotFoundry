"""One error format for the whole API:  {"error": {"code": "...", "message": "...", "details": ...}}"""
import logging
import sqlite3
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..storage.database import StorageError

log = logging.getLogger(__name__)

_STATUS_CODES = {401: "unauthorized", 404: "not_found", 405: "method_not_allowed"}


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def error_response(status_code: int, code: str, message: str, details: Any = None) -> JSONResponse:
    return JSONResponse(status_code=status_code,
                        content={"error": {"code": code, "message": message, "details": details}})


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return error_response(exc.status_code, _STATUS_CODES.get(exc.status_code, "http_error"), str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # only location + message: never echo the submitted values back
        problems = [{"location": [str(p) for p in e["loc"]], "message": e["msg"]} for e in exc.errors()]
        return error_response(422, "invalid_request", "The request is not valid.", {"problems": problems})

    @app.exception_handler(StorageError)
    @app.exception_handler(sqlite3.Error)
    async def _storage_error(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Storage error while handling %s", request.url.path)
        return error_response(500, "storage_error", f"The database reported a problem: {exc}")

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled error while handling %s", request.url.path)
        return error_response(500, "internal_error", "Something went wrong inside the engine. See engine.log.")
