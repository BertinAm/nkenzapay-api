"""One error shape for the whole API, so the client has one thing to parse."""
from rest_framework.views import exception_handler as drf_exception_handler


class DomainError(Exception):
    """A rule the business defined was broken. Carries a stable code."""

    status_code = 400

    def __init__(self, code: str, message: str, detail: dict | None = None):
        self.code = code
        self.message = message
        self.detail = detail or {}
        super().__init__(message)


class Forbidden(DomainError):
    """Refused because of who is asking, not what they asked for.

    Carries 403 rather than the base 400, so a client can tell "you may not do
    this" from "that request was malformed" without reading the message.
    """

    status_code = 403

    def __init__(self, message="You do not have access to that.", code="forbidden"):
        super().__init__(code, message)


class QuoteExpired(DomainError):
    def __init__(self):
        super().__init__("quote_expired", "This quote has expired. Ask for a fresh one.")


class IllegalTransition(DomainError):
    status_code = 409

    def __init__(self, current, target):
        super().__init__(
            "illegal_transition",
            f"A transfer at {current} cannot move to {target}.",
            {"from": current, "to": target},
        )


class ChatLocked(DomainError):
    status_code = 409

    def __init__(self):
        super().__init__("chat_locked", "This transfer is closed. Its chat is read only.")


def api_exception_handler(exc, context):
    if isinstance(exc, DomainError):
        from rest_framework.response import Response

        return Response(
            {"error": {"code": exc.code, "message": exc.message, "detail": exc.detail}},
            status=exc.status_code,
        )

    response = drf_exception_handler(exc, context)
    if response is not None and not isinstance(response.data, dict):
        response.data = {"error": {"code": "error", "message": str(exc), "detail": {}}}
    elif response is not None and "error" not in response.data:
        detail = response.data
        message = detail.get("detail") if isinstance(detail, dict) else str(detail)
        response.data = {
            "error": {
                "code": getattr(exc, "default_code", "error"),
                "message": str(message) if message else "Request failed.",
                "detail": detail if isinstance(detail, dict) else {},
            }
        }
    return response
