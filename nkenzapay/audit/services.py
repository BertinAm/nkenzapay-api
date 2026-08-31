"""Writing to the audit log.

Every financial or identity-touching action goes through record(). Callers pass
a summary written the way the desk should read it back, because a log that says
"transaction.updated" tells nobody anything six months later.
"""
from .models import AuditEntry


def record(*, actor, action, summary, target=None, before=None, after=None, request=None):
    return AuditEntry.objects.create(
        actor=actor if (actor and getattr(actor, "is_authenticated", False)) else None,
        action=action,
        summary=summary[:280],
        target_type=target.__class__.__name__.lower() if target is not None else "",
        target_id=str(getattr(target, "pk", "")) if target is not None else "",
        before=before or {},
        after=after or {},
        ip=client_ip(request),
    )


def client_ip(request):
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")
