"""Idempotency.

A customer on a bad connection taps Create order, sees nothing happen, and taps
again. Without this they get two transfers and pay twice. With it, the second
request returns the first request's response and creates nothing.

The client sends an `Idempotency-Key` header — any unique string, a UUID is
fine. The same key with the same body replays the stored response. The same key
with a *different* body is refused, because that is either a bug or someone
trying to reuse a key to smuggle a second operation through.

Applied by decorating a view. It is deliberately opt-in rather than blanket:
only the operations where a duplicate actually costs something are wrapped, and
being explicit about which those are is worth more than the convenience.
"""
from __future__ import annotations

import functools
import hashlib
import json

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.response import Response

from .models import EventKind, IdempotencyKey
from . import services

HEADER = "HTTP_IDEMPOTENCY_KEY"
MAX_KEY_LENGTH = 200


def fingerprint(request) -> str:
    """Hash the body so the same key cannot carry different content."""
    body = request.body or b""
    return hashlib.sha256(body).hexdigest()


def candidate_scopes(request) -> list[str]:
    """The scopes a key could have been stored under, most specific first.

    Keys are scoped so one caller cannot replay another's response. Signed in,
    that is the account; signed out, the address.

    Both are checked on lookup because sign-up changes the answer mid-flow: the
    first attempt arrives anonymous and the retry arrives holding the session
    the first attempt created. Matching only the current scope would miss the
    stored response and create a second account, which is the exact thing this
    module exists to prevent.
    """
    scopes = []
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        scopes.append(f"user:{user.pk}")
    scopes.append(f"ip:{services.client_ip(request) or 'unknown'}")
    return scopes


def scope_for(request) -> str:
    return candidate_scopes(request)[0]


def idempotent(view):
    """Make a POST view safe to retry.

    Without a key the view runs normally: the header is a promise the client
    makes, not something the server can invent on their behalf.
    """

    @functools.wraps(view)
    def wrapper(self, request, *args, **kwargs):
        key = (request.META.get(HEADER) or "").strip()
        if not key or request.method not in ("POST", "PUT", "PATCH"):
            return view(self, request, *args, **kwargs)

        if len(key) > MAX_KEY_LENGTH:
            return _error(
                "idempotency_key_too_long",
                "That idempotency key is longer than the platform accepts.",
            )

        scopes = candidate_scopes(request)
        digest = fingerprint(request)

        existing = IdempotencyKey.objects.filter(key=key, scope__in=scopes).first()
        if existing is not None:
            record, first_time = existing, False
        else:
            try:
                with transaction.atomic():
                    record = IdempotencyKey.objects.create(
                        key=key,
                        scope=scopes[0],
                        user=request.user if request.user.is_authenticated else None,
                        method=request.method,
                        path=request.path[:300],
                        request_fingerprint=digest,
                    )
                first_time = True
            except IntegrityError:
                # Two copies of the same request arrived together.
                record = IdempotencyKey.objects.filter(key=key, scope__in=scopes).first()
                first_time = False

        if not first_time:
            if record is None:
                # Lost a race with a cleanup; treat it as a fresh request.
                return view(self, request, *args, **kwargs)

            if record.request_fingerprint != digest:
                services.record(
                    EventKind.IDEMPOTENCY_REPLAY,
                    request=request,
                    summary="Idempotency key reused with a different body",
                    user=request.user,
                    detail={"key": key[:40], "path": request.path},
                )
                return _error(
                    "idempotency_key_reused",
                    "That idempotency key was already used for a different request.",
                    status=409,
                )

            if not record.is_complete:
                # The first attempt is still running. Telling the client to
                # wait is safer than running the operation a second time.
                return _error(
                    "request_in_progress",
                    "That request is still being processed. Try again shortly.",
                    status=409,
                )

            services.record(
                EventKind.IDEMPOTENCY_REPLAY,
                request=request,
                summary=f"Replayed {request.method} {request.path[:60]}",
                user=request.user,
                detail={"key": key[:40]},
            )
            replay = Response(record.response_body, status=record.status_code)
            replay["Idempotent-Replay"] = "true"
            return replay

        response = view(self, request, *args, **kwargs)

        # Only a settled outcome is stored. A 500 should be retryable, because
        # the caller has no idea whether anything happened.
        if 200 <= response.status_code < 500:
            if hasattr(response, "data"):
                try:
                    body = json.loads(json.dumps(response.data, default=str))
                except (TypeError, ValueError):
                    body = None
            else:
                body = None

            IdempotencyKey.objects.filter(pk=record.pk).update(
                status_code=response.status_code,
                response_body=body,
                is_complete=True,
                completed_at=timezone.now(),
            )
        else:
            IdempotencyKey.objects.filter(pk=record.pk).delete()

        return response

    return wrapper


def _error(code, message, status=400):
    return Response(
        {"error": {"code": code, "message": message, "detail": {}}}, status=status
    )
