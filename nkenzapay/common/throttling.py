"""Rate limiting that degrades instead of collapsing.

DRF's throttles keep their counters in the cache and let a cache error escape,
which turns "Redis is briefly unreachable" or "the cache table was never
created" into a 500 on every rate-limited endpoint. That is exactly what
happened here once, and rate limiting is not worth an outage.

These subclasses catch the failure and allow the request. Losing the limit for
the length of a cache outage is the lesser problem, and the security log still
records what is happening because it writes to the database, not the cache.
"""
from __future__ import annotations

import logging

from rest_framework.throttling import (
    AnonRateThrottle,
    ScopedRateThrottle,
    UserRateThrottle,
)

logger = logging.getLogger(__name__)


class FailOpenMixin:
    def allow_request(self, request, view):
        try:
            allowed = super().allow_request(request, view)
        except Exception:  # noqa: BLE001 - a cache fault must not deny service
            logger.warning(
                "Rate limiting is unavailable; allowing the request. "
                "Check the cache backend.",
                exc_info=True,
            )
            return True

        if not allowed:
            self.record_refusal(request, view)
        return allowed

    def record_refusal(self, request, view):
        from nkenzapay.security import services
        from nkenzapay.security.models import EventKind

        services.record(
            EventKind.RATE_LIMITED,
            request=request,
            summary=f"Rate limit reached on {request.path[:80]}",
            user=getattr(request, "user", None),
            detail={"scope": getattr(self, "scope", "") or self.__class__.__name__},
            status_code=429,
        )


class ScopedRate(FailOpenMixin, ScopedRateThrottle):
    """Per-view limits, chosen with `throttle_scope` on the view."""


class AnonRate(FailOpenMixin, AnonRateThrottle):
    """A blanket ceiling for signed-out callers."""


class UserRate(FailOpenMixin, UserRateThrottle):
    """A blanket ceiling per account, well above ordinary use."""
