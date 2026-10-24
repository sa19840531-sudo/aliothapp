from fastapi import FastAPI, Request, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

_H5_URL_VALIDATION_LOCATION = ("body", "target", "h5", "url")


class AliothError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        *,
        error_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_id = error_id


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        if any(
            error.get("type") == "WEB_LEGACY_EXECUTION_OVERRIDE_FORBIDDEN"
            for error in exc.errors()
        ):
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={
                    "code": "WEB_LEGACY_EXECUTION_OVERRIDE_FORBIDDEN",
                    "message": "新 Web Run 不允许覆盖历史执行策略",
                },
            )
        errors = []
        for error in exc.errors():
            if tuple(error.get("loc", ())) != _H5_URL_VALIDATION_LOCATION:
                errors.append(error)
                continue
            sanitized_error = {
                **error,
                "msg": "Value error, H5 URL 无效",
                "input": "[已脱敏]",
            }
            sanitized_error.pop("ctx", None)
            errors.append(sanitized_error)
        return await request_validation_exception_handler(
            request,
            RequestValidationError(errors),
        )

    @app.exception_handler(AliothError)
    async def handle_alioth_error(_: Request, exc: AliothError) -> JSONResponse:
        error_payload = {
            "code": exc.__class__.__name__,
            "message": exc.message,
        }
        if exc.error_id is not None:
            error_payload["error_id"] = exc.error_id
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": error_payload},
        )

