"""FastAPI application for the read-only TechPulse API.

OpenAPI is generated from the route models. The default object store is the
local filesystem backend under the artifacts directory. An S3 backend can
replace that object by implementing ``ObjectStore``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from techpulse.api.auth import (
    SCHEMA_VERSION,
    ApiError,
    ApiSettings,
    RateLimiter,
    accepted_request_id,
)
from techpulse.api.routes import (
    health_router,
    json_error,
    metrics_router,
    reader_router,
)
from techpulse.workflows.activities import LocalObjectStore, ObjectStore

_WRITE_METHODS = frozenset({"post", "put", "patch", "delete", "trace"})


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = accepted_request_id(request.headers.get("x-request-id"))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Schema-Version"] = SCHEMA_VERSION
        return response


class TechPulseAPI(FastAPI):
    """Read API with the section 6 paths and a bearer scheme on /v1."""

    def openapi(self) -> dict[str, Any]:
        cached = self.openapi_schema
        if cached is not None:
            return cached
        schema = get_openapi(
            title=self.title,
            version=self.version,
            summary=self.summary,
            description=self.description,
            routes=self.routes,
        )
        _annotate_openapi(schema)
        self.openapi_schema = schema
        return schema


def create_app(
    settings: ApiSettings | None = None,
    store: ObjectStore | None = None,
) -> FastAPI:
    resolved = settings if settings is not None else ApiSettings.from_env()
    object_store = store if store is not None else LocalObjectStore(resolved.artifacts_dir)
    app = TechPulseAPI(
        title="TechPulse-AI",
        version="1.0",
        summary="Read-only reports, trends, evidence, and system status.",
        description=(
            "Public rows are those named by a report whose Q10 manifest has "
            "qa_pass and signed set. Cursors are HMAC-signed. There are no write routes."
        ),
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = resolved
    app.state.store = object_store
    app.state.limiter = RateLimiter(resolved.rate_limit, resolved.rate_window_seconds)
    app.state.anonymous = RateLimiter(resolved.anon_limit, resolved.rate_window_seconds)
    app.include_router(health_router)
    app.include_router(reader_router)
    app.include_router(metrics_router)
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(RequestValidationError, _handle_validation)
    app.add_middleware(RequestIdMiddleware)
    if resolved.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.cors_origins),
            allow_methods=["GET", "HEAD", "OPTIONS"],
            allow_headers=["Authorization", "X-Request-ID"],
            allow_credentials=False,
            max_age=600,
        )
    return app


def _handle_api_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ApiError):
        return json_error(request, ApiError(500, "error", "request failed"))
    return json_error(request, exc)


def _handle_validation(request: Request, exc: Exception) -> JSONResponse:
    del exc
    return json_error(request, ApiError(400, "invalid_request", "request parameters are invalid"))


def _annotate_openapi(schema: dict[str, Any]) -> None:
    components = schema.setdefault("components", {})
    if not isinstance(components, dict):
        return
    schemes = components.setdefault("securitySchemes", {})
    if isinstance(schemes, dict):
        schemes["ReadToken"] = {
            "type": "http",
            "scheme": "bearer",
            "description": "Rotatable read-only bearer token.",
        }
    paths = schema.get("paths")
    if not isinstance(paths, dict):
        return
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method in list(item):
            if method in _WRITE_METHODS:
                del item[method]
                continue
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            responses = operation.get("responses")
            if isinstance(responses, dict):
                responses.pop("422", None)
            if isinstance(path, str) and path.startswith("/v1/"):
                operation["security"] = [{"ReadToken": []}]
    nested = components.get("schemas")
    if isinstance(nested, dict):
        nested.pop("HTTPValidationError", None)
        nested.pop("ValidationError", None)


app = create_app()
