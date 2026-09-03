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
    """The caller's address, resolved the same way the security log resolves it.

    This used to read X-Forwarded-For directly. Behind Cloudflare that is
    forgeable: Cloudflare appends the real address to whatever the client sent,
    so the leftmost value — the one this returned — was attacker-controlled.
    Every audit row could be stamped with an address of the caller's choosing.

    TRUSTED_IP_HEADERS names the one header the proxy in front of us always
    overwrites, and nothing else is believed.
    """
    # Imported here rather than at module scope: security.views imports this
    # module, and a top-level import would close the loop.
    from nkenzapay.security.services import client_ip as trusted_client_ip

    return trusted_client_ip(request)
