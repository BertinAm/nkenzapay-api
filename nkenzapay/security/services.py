"""Recording and reacting to security events.

Two jobs. Write down what happened in enough detail that the desk can act on
it, and block an address that is clearly attacking rather than fumbling.

Everything here is best effort. A failure to record must never fail the request
it was recording — the alternative is a logging bug that takes the site down.
"""
from __future__ import annotations

import ipaddress
import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .models import BlockedAddress, EventKind, SecurityEvent, Severity

logger = logging.getLogger(__name__)

# How many events of one kind from one address before it is blocked, and for
# how long. Tuned so a person mistyping a password is never blocked and a
# script trying a thousand is blocked in seconds.
#
# The defaults are published with this code, so anyone can read them and stay
# just underneath. Deployments should tighten them from the environment —
# SECURITY_THRESHOLDS is a JSON object of {event kind: hits} — which is why the
# numbers a given install actually uses are not in this file.
_DEFAULT_THRESHOLDS = {
    EventKind.INJECTION_PROBE: (3, timedelta(hours=24)),
    EventKind.TRAVERSAL_PROBE: (3, timedelta(hours=24)),
    EventKind.SCANNER: (12, timedelta(hours=6)),
    EventKind.LOGIN_FAILED: (20, timedelta(hours=1)),
    EventKind.CSRF_FAILED: (15, timedelta(hours=1)),
    EventKind.REGISTRATION_ABUSE: (6, timedelta(hours=6)),
    EventKind.PASSWORD_RESET_ABUSE: (10, timedelta(hours=2)),
    EventKind.RATE_LIMITED: (40, timedelta(hours=1)),
    EventKind.ENUMERATION: (10, timedelta(hours=2)),
}


def _thresholds():
    overrides = getattr(settings, "SECURITY_THRESHOLDS", None) or {}
    if not overrides:
        return _DEFAULT_THRESHOLDS

    merged = dict(_DEFAULT_THRESHOLDS)
    for kind, hits in overrides.items():
        if kind in merged:
            merged[kind] = (int(hits), merged[kind][1])
    return merged


DEFAULT_SEVERITY = {
    EventKind.INJECTION_PROBE: Severity.CRITICAL,
    EventKind.TRAVERSAL_PROBE: Severity.CRITICAL,
    EventKind.BLOCKED: Severity.HIGH,
    EventKind.SCANNER: Severity.MEDIUM,
    EventKind.LOGIN_LOCKED: Severity.HIGH,
    EventKind.CSRF_FAILED: Severity.MEDIUM,
    EventKind.PERMISSION_DENIED: Severity.MEDIUM,
    EventKind.ADMIN_ACTION_DENIED: Severity.HIGH,
    EventKind.BAD_UPLOAD: Severity.MEDIUM,
    EventKind.REGISTRATION_ABUSE: Severity.MEDIUM,
    EventKind.PASSWORD_RESET_ABUSE: Severity.MEDIUM,
    EventKind.ENUMERATION: Severity.MEDIUM,
    EventKind.QUOTE_ABUSE: Severity.LOW,
    EventKind.RATE_LIMITED: Severity.LOW,
    EventKind.LOGIN_FAILED: Severity.LOW,
    EventKind.LOGIN_NEW_DEVICE: Severity.INFO,
    EventKind.IDEMPOTENCY_REPLAY: Severity.INFO,
}

# Trimmed before storage. A probe payload is untrusted text that a desk
# operator will read in a browser, and there is no reason to keep a megabyte.
MAX_DETAIL_CHARS = 500


def is_infrastructure(ip) -> bool:
    """Loopback and private addresses are never blocked automatically.

    If TRUSTED_IP_HEADERS is unset behind a reverse proxy, every request appears
    to come from 127.0.0.1 — and one attacker would get the entire site blocked
    for everybody. Refusing to auto-block these ranges turns a total outage into
    a misconfiguration that shows up in the security log instead.

    A person can still block one deliberately.
    """
    if not ip:
        return True
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
    )


def client_ip(request) -> str | None:
    """The caller's address, trusting only the proxy we actually run behind.

    Behind Cloudflare, CF-Connecting-IP is the one header a client cannot
    forge. X-Forwarded-For can be, so its leftmost value is only used when no
    trusted header is present and it is explicitly enabled.
    """
    if request is None:
        return None

    for header in settings.TRUSTED_IP_HEADERS:
        value = request.META.get(header)
        if value:
            return value.split(",")[0].strip()

    return request.META.get("REMOTE_ADDR")


def record(
    kind,
    *,
    request=None,
    summary="",
    severity=None,
    user=None,
    identifier="",
    detail=None,
    status_code=None,
):
    """Write one event, and block the address if it has crossed a threshold."""
    try:
        ip = client_ip(request)
        event = SecurityEvent.objects.create(
            kind=kind,
            severity=severity or DEFAULT_SEVERITY.get(kind, Severity.LOW),
            summary=(summary or EventKind(kind).label)[:280],
            ip=ip,
            user=user if (user is not None and getattr(user, "is_authenticated", False)) else None,
            identifier=str(identifier)[:190],
            method=(request.method if request else "")[:8],
            path=(request.path if request else "")[:300],
            status_code=status_code,
            user_agent=(request.META.get("HTTP_USER_AGENT", "") if request else "")[:600],
            referer=(request.META.get("HTTP_REFERER", "") if request else "")[:300],
            country=(request.META.get("HTTP_CF_IPCOUNTRY", "") if request else "")[:2],
            detail=_trim(detail or {}),
        )
    except Exception:  # noqa: BLE001 - recording must not break the request
        logger.exception("Could not record security event %s", kind)
        return None

    try:
        _maybe_block(kind, ip)
    except Exception:  # noqa: BLE001
        logger.exception("Could not evaluate auto-block for %s", ip)

    return event


def _trim(detail: dict) -> dict:
    trimmed = {}
    for key, value in list(detail.items())[:20]:
        text = value if isinstance(value, (int, float, bool)) else str(value)
        if isinstance(text, str) and len(text) > MAX_DETAIL_CHARS:
            text = text[:MAX_DETAIL_CHARS] + "…"
        trimmed[str(key)[:40]] = text
    return trimmed


def _maybe_block(kind, ip):
    if not ip:
        return
    if is_infrastructure(ip):
        # Almost always the proxy rather than the caller. Blocking it would
        # take the site down for everyone; the event is still recorded.
        logger.warning(
            "Not auto-blocking %s for %s: the address looks like infrastructure. "
            "If this site sits behind a proxy, set TRUSTED_IP_HEADERS.",
            ip, kind,
        )
        return

    rule = _thresholds().get(kind)
    if rule is None:
        return

    threshold, duration = rule
    window = timezone.now() - timedelta(hours=1)
    hits = SecurityEvent.objects.filter(ip=ip, kind=kind, at__gte=window).count()
    if hits < threshold:
        return

    block(
        ip,
        reason=f"{hits} × {EventKind(kind).label.lower()} in an hour",
        kind=kind,
        duration=duration,
    )


def block(ip, *, reason, kind="", duration=None, actor=None):
    """Refuse an address. Automatic blocks expire; a person can make one stick."""
    expires_at = timezone.now() + duration if duration else None
    entry, created = BlockedAddress.objects.get_or_create(
        ip=ip,
        defaults={
            "reason": reason[:280],
            "kind": kind,
            "expires_at": expires_at,
            "is_automatic": actor is None,
            "blocked_by": actor,
        },
    )
    if not created:
        entry.hits += 1
        entry.reason = reason[:280]
        # Never shorten an existing block, and never override a manual one.
        if entry.is_automatic and actor is None and expires_at and entry.expires_at:
            entry.expires_at = max(entry.expires_at, expires_at)
        elif actor is not None:
            entry.is_automatic = False
            entry.blocked_by = actor
            entry.expires_at = expires_at
        entry.save()

    _forget(ip)
    if created:
        logger.warning("Blocked %s: %s", ip, reason)
    return entry


def unblock(ip, actor=None):
    BlockedAddress.objects.filter(ip=ip).delete()
    _forget(ip)
    logger.info("Unblocked %s by %s", ip, getattr(actor, "email", "system"))


def _forget(ip):
    try:
        cache.delete(_block_cache_key(ip))
    except Exception:  # noqa: BLE001
        pass


def _block_cache_key(ip):
    return f"sec:block:{ip}"


def is_blocked(ip) -> bool:
    """Checked on every request, so the answer is cached briefly.

    Sixty seconds means an unblock takes effect within a minute, which is fast
    enough for a person waiting on it and slow enough to keep the database out
    of the hot path.

    The cache is treated as an optimisation, not a dependency. A missing cache
    table or an unreachable Redis falls through to the database; it must never
    turn into a 500 on every request, which is exactly what happens if this
    lets the exception out.
    """
    if not ip:
        return False

    try:
        cached = cache.get(_block_cache_key(ip))
        if cached is not None:
            return cached
    except Exception:  # noqa: BLE001
        logger.warning("Block cache unavailable; falling back to the database.")

    try:
        entry = BlockedAddress.objects.filter(ip=ip).first()
    except Exception:  # noqa: BLE001 - a broken query must not deny everyone
        logger.exception("Could not read the blocklist; allowing the request.")
        return False

    blocked = bool(entry and entry.is_active)

    # An expired block is cleared as it is noticed, so the table stays small.
    if entry and not entry.is_active:
        entry.delete()
        blocked = False

    try:
        cache.set(_block_cache_key(ip), blocked, 60)
    except Exception:  # noqa: BLE001
        pass

    return blocked


def count_in_window(ip, kind, minutes=60) -> int:
    since = timezone.now() - timedelta(minutes=minutes)
    return SecurityEvent.objects.filter(ip=ip, kind=kind, at__gte=since).count()
