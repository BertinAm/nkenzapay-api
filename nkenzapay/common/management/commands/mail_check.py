"""Say whether this deployment can actually send an email, and if not, why.

`notify` must never take a transfer down because a mail server is unhappy, so
it catches everything and writes a log line. That is the right call and it has
a cost: a deployment where mail is quietly going nowhere looks exactly like one
where it is working. Nobody finds out until a customer says they never got the
link.

    python manage.py mail_check you@example.com

Reports the settings that are actually in force, then tries a real send and
prints whatever went wrong in full rather than a one-line summary.
"""
from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Check that email can leave this server, and report why if it cannot."

    def add_arguments(self, parser):
        parser.add_argument("to", help="Where to send the test message.")
        parser.add_argument(
            "--settings-only",
            action="store_true",
            help="Print the configuration without sending anything.",
        )

    def handle(self, *args, **options):
        self.report_settings()
        if options["settings_only"]:
            return
        self.stdout.write("")
        self.attempt(options["to"])

    def report_settings(self):
        backend = settings.EMAIL_BACKEND
        self.stdout.write(self.style.MIGRATE_HEADING("What is configured"))
        for name, value in [
            ("EMAIL_BACKEND", backend),
            ("EMAIL_HOST", settings.EMAIL_HOST or "(empty)"),
            ("EMAIL_PORT", settings.EMAIL_PORT),
            ("EMAIL_HOST_USER", settings.EMAIL_HOST_USER or "(empty)"),
            # Length only. The value itself has no business in a terminal
            # somebody might screenshot or paste into a support thread.
            ("EMAIL_HOST_PASSWORD", self.secret(settings.EMAIL_HOST_PASSWORD)),
            ("EMAIL_USE_TLS", settings.EMAIL_USE_TLS),
            ("EMAIL_USE_SSL", settings.EMAIL_USE_SSL),
            ("EMAIL_TIMEOUT", settings.EMAIL_TIMEOUT),
            ("DEFAULT_FROM_EMAIL", settings.DEFAULT_FROM_EMAIL),
            ("SITE_URL", settings.SITE_URL),
        ]:
            self.stdout.write(f"  {name:<22} {value}")

        if "console" in backend or "dummy" in backend or "locmem" in backend:
            self.stdout.write("")
            self.stdout.write(self.style.ERROR(
                "This backend does not send anything. Mail is written to the "
                "log (console), discarded (dummy) or kept in memory (locmem). "
                "Every welcome, confirmation and reset link on this deployment "
                "has gone nowhere. Set EMAIL_BACKEND in .env to "
                "django.core.mail.backends.smtp.EmailBackend and restart."
            ))

    def attempt(self, to):
        self.stdout.write(self.style.MIGRATE_HEADING(f"Sending to {to}"))
        try:
            # A connection of its own, opened explicitly, so a failure to reach
            # the server is reported here rather than at send time where it is
            # harder to tell apart from a rejected message.
            connection = get_connection(fail_silently=False)
            connection.open()
        except Exception as exc:  # noqa: BLE001 - the whole point is to show it
            self.fail("The mail server could not be reached.", exc)
            return

        message = EmailMultiAlternatives(
            subject="NkenzaPay mail check",
            body=(
                "This is the mail check from manage.py mail_check.\n\n"
                "If you are reading it, this deployment can send email."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[to],
            connection=connection,
        )
        try:
            sent = message.send(fail_silently=False)
        except Exception as exc:  # noqa: BLE001
            self.fail("The server was reached but refused the message.", exc)
            return
        finally:
            connection.close()

        if not sent:
            self.stdout.write(self.style.ERROR(
                "  The backend reported that nothing was sent, without raising."
            ))
            return

        self.stdout.write(self.style.SUCCESS(f"  Accepted for delivery to {to}."))
        self.stdout.write(
            "  Accepted is not the same as arrived. If it does not turn up, the "
            "message left this server and the question moves to SPF, DKIM and "
            "DMARC on the sending domain."
        )

    def fail(self, headline, exc):
        self.stdout.write(self.style.ERROR(f"  {headline}"))
        self.stdout.write(f"  {type(exc).__name__}: {exc}")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING(
            "  This is the error that notify() catches and writes to the log, "
            "which is why sign-up looks like it worked and no email arrives."
        ))

    @staticmethod
    def secret(value):
        return f"set, {len(value)} characters" if value else "(empty)"
