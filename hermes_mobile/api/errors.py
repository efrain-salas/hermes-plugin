from __future__ import annotations

from aiohttp import web


class MobileError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        *,
        retryable: bool = False,
        details: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
        self.details = details or {}


def error_response(error: MobileError, request_id: str) -> web.Response:
    return web.json_response(
        {
            "error": {
                "code": error.code,
                "message": error.message,
                "request_id": request_id,
                "retryable": error.retryable,
                "details": error.details,
            }
        },
        status=error.status,
        headers={"X-Request-Id": request_id},
    )
