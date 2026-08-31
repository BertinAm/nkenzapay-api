"""First-party page-view recording.

Only GET requests to the front end are counted, and only when the response
succeeded. API traffic, static files and admin routes are skipped — a desk
operator refreshing the queue should not look like visitor demand.
"""
from __future__ import annotations

import re

from .models import PageView

SKIP_PREFIXES = ("/api/", "/static/", "/media/", "/django-admin/", "/ws/", "/favicon")

MOBILE_HINT = re.compile(r"iphone|android|ipod|windows phone", re.I)
TABLET_HINT = re.compile(r"ipad|tablet", re.I)


class PageViewMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            self.record(request, response)
        except Exception:  # noqa: BLE001 - analytics must never break a page
            pass
        return response

    def record(self, request, response):
        if request.method != "GET" or response.status_code >= 400:
            return
        path = request.path
        if path.startswith(SKIP_PREFIXES):
            return

        agent = request.META.get("HTTP_USER_AGENT", "")
        if TABLET_HINT.search(agent):
            device = PageView.TABLET
        elif MOBILE_HINT.search(agent):
            device = PageView.MOBILE
        else:
            device = PageView.DESKTOP

        if not request.session.session_key:
            request.session.save()

        referrer = request.META.get("HTTP_REFERER", "")[:300]
        PageView.objects.create(
            path=path[:200],
            session_key=request.session.session_key or "",
            user=request.user if request.user.is_authenticated else None,
            referrer=referrer,
            source=classify_source(referrer),
            device=device,
        )


def classify_source(referrer: str) -> str:
    """Group referrers into the buckets the analytics screen shows."""
    if not referrer:
        return "direct"
    host = referrer.split("//")[-1].split("/")[0].lower()
    table = {
        "whatsapp": ("whatsapp", "wa.me", "web.whatsapp"),
        "search": ("google.", "bing.", "duckduckgo", "yahoo."),
        "social": ("facebook", "instagram", "twitter", "x.com", "t.co", "linkedin", "tiktok"),
    }
    for label, needles in table.items():
        if any(n in host for n in needles):
            return label
    return "referral"
