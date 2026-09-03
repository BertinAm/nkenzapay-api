"""Deployment checks.

Every one of these exists because the thing it looks for went wrong once, and
went wrong at runtime, where the cost is an outage or a quiet leak rather than
a line of output. Moving the discovery to `manage.py check --deploy` is the
whole point: a deploy that is about to serve every request as a 500, or write
customers' documents to a shared disk in the clear, should say so while there
is still a terminal open.

They are registered as deploy checks, so they run under `--deploy` and stay out
of the way of ordinary commands and the test run.

    python manage.py check --deploy

Run it as the last step of every deploy. See DEPLOYMENT.md.
"""
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.checks import Error, Warning, register

# Directory names a web server serves out of. If the media root is under one of
# these, the files are one URL away from the public internet.
WEB_SERVED = {"public_html", "www", "htdocs", "public", "web", "wwwroot"}


@register("nkenzapay", deploy=True)
def check_media_is_encrypted(app_configs, **kwargs):
    """Files on a disk we do not own must not be readable.

    On shared hosting the disk is read by the host's staff, copied by their
    backup system, and reachable from anything else on the account. The
    encryption key is what makes that survivable, so a deployment without one
    is not a warning.
    """
    from . import crypto

    if getattr(settings, "MEDIA_STORAGE", "local") != "local":
        return []

    try:
        configured = crypto.is_configured()
    except Exception as exc:  # noqa: BLE001 - a malformed key is its own error
        return [Error(str(exc), id="nkenzapay.E001")]

    if configured:
        return []

    return [
        Error(
            "Uploaded files would be written to disk in the clear.",
            hint=(
                "Payment evidence and photographs of customers end up in "
                f"{settings.MEDIA_ROOT}. Generate a key with "
                "'manage.py generate_media_key' and set MEDIA_ENCRYPTION_KEY. "
                "Keep it out of any backup that also holds the files - a key "
                "stored beside what it protects is decoration."
            ),
            id="nkenzapay.E002",
        )
    ]


@register("nkenzapay", deploy=True)
def check_media_root_is_not_web_served(app_configs, **kwargs):
    """A private media directory inside the document root is not private."""
    root = Path(settings.MEDIA_ROOT).resolve()
    served = [part for part in root.parts if part.lower() in WEB_SERVED]
    if not served:
        return []

    return [
        Error(
            f"MEDIA_ROOT is inside '{served[0]}', which the web server serves.",
            hint=(
                f"{root} holds payment evidence. Move it above the document "
                "root - a sibling of it, not a child - and set MEDIA_ROOT to "
                "the new path. The .htaccess written into the directory is a "
                "second line of defence, not the first."
            ),
            id="nkenzapay.E003",
        )
    ]


@register("nkenzapay", deploy=True)
def check_media_root_is_outside_the_app(app_configs, **kwargs):
    """On cPanel the application root is usually the document root too.

    Which means the default MEDIA_ROOT, a directory inside the project, sits
    under the web root without any of the giveaway names the check above looks
    for. The .htaccess written into it denies everything, and Passenger hands
    most requests to Django rather than serving files, so this is a warning
    rather than an error. It is still the wrong place to put photographs of
    customers' faces when moving it costs one environment variable.
    """
    from django.conf import settings as django_settings

    root = Path(django_settings.MEDIA_ROOT).resolve()
    base = Path(django_settings.BASE_DIR).resolve()

    if not root.is_relative_to(base):
        return []

    return [
        Warning(
            "MEDIA_ROOT is inside the application directory.",
            hint=(
                f"{root} sits under {base}, which on cPanel is also the "
                "document root. Set MEDIA_ROOT to a directory beside the "
                "application rather than inside it, for example "
                "/home/USER/nkenzapay-media, and move what is already there."
            ),
            id="nkenzapay.W010",
        )
    ]


@register("nkenzapay", deploy=True)
def check_the_cache_works(app_configs, **kwargs):
    """A cache that raises turns rate limiting into a 500 on every request.

    Blocking, the login lockout and throttling all fail open now, so this is no
    longer fatal - but running without a cache means running with no rate
    limiting at all, and nobody notices a limit that silently is not there.
    """
    from django.core.cache import cache

    probe = "nkenzapay:check"
    try:
        cache.set(probe, "ok", 5)
        value = cache.get(probe)
        cache.delete(probe)
    except Exception as exc:  # noqa: BLE001
        return [
            Error(
                f"The cache is not usable: {exc}",
                hint=(
                    "With a database cache, run 'manage.py createcachetable'. "
                    "Rate limiting and address blocking read the cache on every "
                    "request; without it they fail open and the site runs with "
                    "no limits at all."
                ),
                id="nkenzapay.E004",
            )
        ]

    if value != "ok":
        return [
            Warning(
                "The cache accepted a write and did not return it.",
                hint=(
                    "A dummy or misconfigured cache backend. Rate limiting will "
                    "never refuse anything."
                ),
                id="nkenzapay.W005",
            )
        ]
    return []


@register("nkenzapay", deploy=True)
def check_the_client_address_is_knowable(app_configs, **kwargs):
    """Behind a proxy with no trusted header, every caller looks the same.

    Two failures come out of that. Blocking stops working, because everybody
    shares one address - and if it did work, one attacker would take the whole
    site down with them. That is why loopback and private ranges are never
    auto-blocked, and why this check exists to say the situation out loud.
    """
    if settings.TRUSTED_IP_HEADERS:
        return []

    return [
        Warning(
            "TRUSTED_IP_HEADERS is empty, so REMOTE_ADDR is the caller's address.",
            hint=(
                "Right if the application is reached directly. Wrong behind "
                "Cloudflare or any reverse proxy, where every request appears "
                "to come from the proxy: rate limits are then shared by "
                "everyone at once and blocking cannot single anybody out. "
                "Behind Cloudflare set TRUSTED_IP_HEADERS=HTTP_CF_CONNECTING_IP. "
                "Set it only to a header your own proxy writes and always "
                "overwrites, or callers can forge it and walk past a block. "
                "Silence this with SILENCED_SYSTEM_CHECKS=nkenzapay.W006 once "
                "the answer is deliberate."
            ),
            id="nkenzapay.W006",
        )
    ]


@register("nkenzapay", deploy=True)
def check_mail_can_actually_be_sent(app_configs, **kwargs):
    """Selecting the SMTP backend is not the same as configuring it.

    Django defaults EMAIL_HOST to localhost and EMAIL_PORT to 25 with no
    authentication. With the SMTP backend selected and nothing else set, mail
    is handed to a local server that may not exist and the failure is a log
    line nobody reads — while the customer waits for a password reset that is
    never coming.
    """
    if "smtp" not in settings.EMAIL_BACKEND:
        return []

    problems = []

    if not settings.EMAIL_HOST:
        problems.append(
            Error(
                "The SMTP email backend is selected but EMAIL_HOST is empty.",
                hint=(
                    "Django will post to localhost:25 without authentication. "
                    "Set EMAIL_HOST, EMAIL_PORT, EMAIL_HOST_USER and "
                    "EMAIL_HOST_PASSWORD, or switch EMAIL_BACKEND back to the "
                    "console backend until you have them."
                ),
                id="nkenzapay.E013",
            )
        )

    if settings.EMAIL_USE_TLS and settings.EMAIL_USE_SSL:
        problems.append(
            Error(
                "EMAIL_USE_TLS and EMAIL_USE_SSL are both on.",
                hint=(
                    "They are different things and Django refuses both at once. "
                    "Port 587 wants TLS; port 465 wants SSL."
                ),
                id="nkenzapay.E014",
            )
        )

    if settings.EMAIL_HOST and not (settings.EMAIL_USE_TLS or settings.EMAIL_USE_SSL):
        problems.append(
            Warning(
                "Mail is sent to an external host without TLS or SSL.",
                hint=(
                    "The password in EMAIL_HOST_PASSWORD, and every reset link "
                    "the platform sends, would cross the network in the clear. "
                    "Silence this with SILENCED_SYSTEM_CHECKS=nkenzapay.W015 "
                    "only if the host is on this machine."
                ),
                id="nkenzapay.W015",
            )
        )

    return problems


@register("nkenzapay", deploy=True)
def check_proxy_secret_is_usable(app_configs, **kwargs):
    """The shared secret that lets the front end vouch for a caller's address.

    Only meaningful when the front end proxies /api here. When it does and this
    is unset, every visitor arrives as the proxy: rate limits are pooled across
    all of them at once, and an automatic block takes the whole site off the
    air rather than one attacker.
    """
    secret = getattr(settings, "PROXY_SHARED_SECRET", "")
    if not secret:
        return []

    problems = []

    if len(secret) < 32:
        problems.append(
            Error(
                "PROXY_SHARED_SECRET is shorter than 32 characters.",
                hint=(
                    "It is the only thing separating a forged X-Client-IP from "
                    "a real one, and a forged one lifts a block. Generate it "
                    "with 'manage.py generate_secret_key'."
                ),
                id="nkenzapay.E011",
            )
        )

    if secret == settings.SECRET_KEY:
        problems.append(
            Error(
                "PROXY_SHARED_SECRET is the same value as SECRET_KEY.",
                hint=(
                    "This one is handed to the front end, which puts a key that "
                    "signs sessions into a second system. Generate a separate "
                    "value."
                ),
                id="nkenzapay.E012",
            )
        )

    return problems


@register("nkenzapay", deploy=True)
def check_secrets_are_real(app_configs, **kwargs):
    problems = []

    if settings.SECRET_KEY.startswith("dev-only"):
        problems.append(
            Error(
                "SECRET_KEY is still the development placeholder.",
                hint=(
                    "It signs sessions and upload links, and anyone reading "
                    "this repository knows the value. Generate one and set it "
                    "in .env."
                ),
                id="nkenzapay.E007",
            )
        )

    media_key = getattr(settings, "MEDIA_ENCRYPTION_KEY", "")
    if media_key and media_key == settings.SECRET_KEY:
        problems.append(
            Error(
                "MEDIA_ENCRYPTION_KEY is the same value as SECRET_KEY.",
                hint=(
                    "One leak would then cost both, and rotating either would "
                    "break the other. Generate a separate key with "
                    "'manage.py generate_media_key'."
                ),
                id="nkenzapay.E008",
            )
        )

    return problems


@register("nkenzapay", deploy=True)
def check_retention_is_a_decision(app_configs, **kwargs):
    """Held data is the risk that grows on its own."""
    if getattr(settings, "MEDIA_RETENTION_DAYS", 0):
        return []

    return [
        Warning(
            "MEDIA_RETENTION_DAYS is 0, so attachments are kept indefinitely.",
            hint=(
                "Every payment screenshot and identity photograph stays on disk "
                "for the life of the deployment, and the pile only grows. Set a "
                "number of days after a transfer closes and run "
                "'manage.py sweep_media' nightly. Check what your own record-"
                "keeping obligations require before choosing the number. "
                "Silence this with SILENCED_SYSTEM_CHECKS=nkenzapay.W009 if "
                "indefinite is the deliberate answer."
            ),
            id="nkenzapay.W009",
        )
    ]
