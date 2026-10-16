import uuid
from typing import Callable
from django.http import HttpRequest, HttpResponse


class CorrelationIdMiddleware:
    """
    Extracts or generates an X-Request-ID header on every inbound request,
    attaching it to request.request_id and echoing it back on the HTTP response.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        correlation_id = (
            request.headers.get("X-Request-ID")
            or request.headers.get("X-Correlation-ID")
            or str(uuid.uuid4())
        )
        request.request_id = correlation_id

        response = self.get_response(request)
        response["X-Request-ID"] = correlation_id
        return response