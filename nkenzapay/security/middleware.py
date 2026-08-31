"""Security middleware.

Three layers, in order:

1. **Blocklist** — an address the platform has already refused gets a 403 before
   anything else runs.
2. **Probe detection** — the request line and query string are checked for the
   patterns a scanner sends. This is not a web application firewall and does not
   pretend to be; it is a tripwire that records who is knocking. The actual
   defences are the ORM, the template escaping and the upload validation.
3. **Response watching** — a burst of 404s or 403s from one address is what
   scanning looks like from the inside.

Detection runs on the request line and query string only. Bodies are left alone:
parsing them here would mean consuming the stream before the view does, and a
legitimate chat message about "SELECT a payment method" is not an attack.
"""
from __future__ import annotations

import re
from urllib.parse import unquote_plus

from django.http import JsonResponse

from . import services
from .models import EventKind, Severity

# Patterns a person does not type by accident. Kept narrow on purpose — a rule
# that fires on ordinary text produces noise, and noise is why security logs
# stop being read.
INJECTION_PATTERNS = [
    (re.compile(r"(?i)\bunion\s+(all\s+)?select\b"), "SQL union select"),
    (re.compile(r"(?i)\bor\s+1\s*=\s*1\b"), "SQL always-true clause"),
    (re.compile(r"(?i);\s*(drop|truncate|delete)\s+(table|from)\b"), "SQL destructive statement"),
    (re.compile(r"(?i)\bsleep\s*\(\s*\d+\s*\)"), "SQL time-based probe"),
    (re.compile(r"(?i)\bbenchmark\s*\("), "SQL benchmark probe"),
    (re.compile(r"(?i)<script[\s>]"), "inline script tag"),
    (re.compile(r"(?i)\bon(error|load|click)\s*="), "inline event handler"),
    (re.compile(r"(?i)javascript:"), "javascript URL"),
    (re.compile(r"(?i)\$\{jndi:"), "JNDI lookup"),
    (re.compile(r"(?i)\{\{.*constructor.*\}\}"), "template injection"),
    (re.compile(r"(?i)\b(etc/passwd|/proc/self/environ)\b"), "system file access"),
]

TRAVERSAL_PATTERN = re.compile(r"(\.\./|\.\.\\|%2e%2e[/\\]|%252e%252e)", re.I)

# Paths that only exist on software NkenzaPay does not run. Anything asking for
# them is looking for a different site's vulnerabilities.
SCANNER_PATHS = re.compile(
    r"(?i)^/(wp-admin|wp-login|wp-content|wordpress|xmlrpc\.php|\.env|\.git|"
    r"phpmyadmin|admin\.php|administrator|cgi-bin|vendor/phpunit|"
    r"config\.json|backup\.sql|\.aws|\.ssh|shell\.php|eval-stdin\.php)"
)

# Legitimate crawlers. Recorded, never blocked.
FRIENDLY_AGENTS = re.compile(
    r"(?i)(googlebot|bingbot|duckduckbot|slurp|applebot|uptimerobot|pingdom)"
)


class SecurityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        ip = services.client_ip(request)
        request.client_ip = ip

        if services.is_blocked(ip):
            services.record(
                EventKind.BLOCKED,
                request=request,
                summary="Request from a blocked address",
                status_code=403,
            )
            return self.refused()

        probe = self.inspect(request)
        if probe is not None:
            kind, description, sample = probe
            services.record(
                kind,
                request=request,
                summary=description,
                detail={"pattern": description, "sample": sample},
                status_code=403,
            )
            return self.refused()

        response = self.get_response(request)
        self.watch(request, response)
        return response

    def inspect(self, request):
        """Look at the path and the query string, encoded and decoded.

        Checking only the raw URL misses everything, because a probe arrives
        percent-encoded: "UNION SELECT" is on the wire as "UNION%20SELECT".
        Decoding twice catches the double-encoding trick that is used precisely
        to slip past a filter that decodes once.
        """
        raw = request.get_full_path()
        once = unquote_plus(raw)
        twice = unquote_plus(once)

        for target in {raw, once, twice}:
            if TRAVERSAL_PATTERN.search(target):
                return EventKind.TRAVERSAL_PROBE, "Path traversal attempt", target[:200]

            for pattern, description in INJECTION_PATTERNS:
                match = pattern.search(target)
                if match:
                    return EventKind.INJECTION_PROBE, description, match.group(0)[:120]

        return None

    def watch(self, request, response):
        """A burst of misses from one address is what scanning looks like."""
        status = getattr(response, "status_code", 200)
        agent = request.META.get("HTTP_USER_AGENT", "")

        if status == 404 and SCANNER_PATHS.match(request.path):
            if not FRIENDLY_AGENTS.search(agent):
                services.record(
                    EventKind.SCANNER,
                    request=request,
                    summary=f"Probed {request.path[:80]}, which this site does not run",
                    status_code=404,
                )
            return

        if status == 403 and request.path.startswith("/api/"):
            # Permission denials on the API are worth seeing; the desk's own
            # 403s are recorded where the decision is made, with more context.
            services.record(
                EventKind.PERMISSION_DENIED,
                request=request,
                summary=f"Refused {request.method} {request.path[:80]}",
                user=getattr(request, "user", None),
                status_code=403,
                severity=Severity.LOW,
            )

    def refused(self):
        return JsonResponse(
            {
                "error": {
                    "code": "refused",
                    "message": "This request was refused.",
                    "detail": {},
                }
            },
            status=403,
        )
