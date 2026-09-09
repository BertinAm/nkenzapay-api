"""Show which exchange rate provider is live, and switch it.

The desk's Rates and fees screen is the normal way to do this and stays so. This
exists for the case that screen cannot cover: a host with no shell, a provider
that has just started failing, and a bootstrap where the row has only arrived by
migration and nothing is pointing at it yet.

    python manage.py rate_provider                        # what is live
    python manage.py rate_provider --use exchangerate_api # switch to it

Switching is written to the audit log, the same as it would be from the desk,
because "who moved us onto that rate and when" is a question somebody will ask.
"""
from django.core.management.base import BaseCommand

from nkenzapay.audit import services as audit
from nkenzapay.rates.models import RateProvider
from nkenzapay.rates.providers import PROVIDERS, credentials_missing


class Command(BaseCommand):
    help = "Show or switch the live exchange rate provider."

    def add_arguments(self, parser):
        parser.add_argument("--use", default="", help="Slug of the provider to make live.")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Switch even though the provider's credentials are missing.",
        )

    def handle(self, *args, **options):
        if options["use"]:
            self.switch(options["use"], force=options["force"])
        self.show()

    def show(self):
        rows = RateProvider.objects.order_by("slug")
        if not rows:
            self.stdout.write(self.style.ERROR(
                "No providers exist at all. Run migrate, then seed."
            ))
            return

        self.stdout.write(self.style.MIGRATE_HEADING("Providers"))
        for row in rows:
            missing = credentials_missing(row.slug)
            state = []
            if row.slug == "mock":
                state.append("hard-coded figures, not a market rate")
            if missing:
                state.append("missing " + ", ".join(missing))
            elif row.slug != "mock" and row.slug in PROVIDERS:
                if not PROVIDERS[row.slug].needs:
                    state.append("no credentials needed")
                else:
                    state.append("credentials set")
            if row.is_active:
                state.append("healthy" if row.is_healthy else "no successful fetch yet")

            mark = "->" if row.is_active else "  "
            note = f"  ({'; '.join(state)})" if state else ""
            self.stdout.write(f" {mark} {row.slug:<20} {row.label}{note}")

        active = rows.filter(is_active=True).first()
        if active is None:
            self.stdout.write("")
            self.stdout.write(self.style.ERROR(
                "Nothing is live, so every quote will be refused."
            ))

    def switch(self, slug, *, force=False):
        provider = RateProvider.objects.filter(slug=slug).first()
        if provider is None:
            known = ", ".join(RateProvider.objects.values_list("slug", flat=True))
            self.stdout.write(self.style.ERROR(f"No provider with slug {slug!r}."))
            self.stdout.write(f"  Known: {known or 'none'}")
            return

        missing = credentials_missing(slug)
        if missing and not force:
            self.stdout.write(self.style.ERROR(
                f"{provider.label} needs {', '.join(missing)} in .env. "
                "Every quote would be refused, so this is not being switched. "
                "Use --force if you are setting the credentials next."
            ))
            return

        was = RateProvider.objects.filter(is_active=True).first()
        if was is not None and was.pk == provider.pk:
            self.stdout.write(f"{provider.label} is already live.")
            return

        RateProvider.objects.exclude(pk=provider.pk).update(is_active=False)
        RateProvider.objects.filter(pk=provider.pk).update(is_active=True)

        audit.record(
            actor=None,
            action="settings.rates_changed",
            summary=(
                f"Rate provider switched to {provider.label} "
                f"from {was.label if was else 'none'} (manage.py rate_provider)"
            ),
            target=provider,
            before={"slug": was.slug if was else None},
            after={"slug": provider.slug},
        )
        self.stdout.write(self.style.SUCCESS(
            f"Switched to {provider.label}."
        ))
        if provider.slug == "mock":
            self.stdout.write(self.style.WARNING(
                "  That is the mock table. Transfers will be priced from "
                "figures in the source, not a market rate."
            ))
        self.stdout.write("")
