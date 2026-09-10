"""Nudge accounts that never finished setting themselves up.

Four messages, then silence: at a day, at three days, then weekly. Somebody who
has ignored four emails is not reading the fifth, and a platform that keeps
sending them is a platform people filter.

Nothing is sent to an account that is finished, waiting on the desk, or
suspended. A rejected document is chased, because that customer is waiting on
themselves and may not have opened the rejection.

    manage.py send_verification_reminders
    manage.py send_verification_reminders --dry-run
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from nkenzapay.accounts.models import Profile
from nkenzapay.notifications import services as notifications

# Reminder number -> how long since the last one (or since signing up).
SCHEDULE = {
    0: timedelta(hours=24),
    1: timedelta(hours=72),
    2: timedelta(days=7),
    3: timedelta(days=7),
}

MAX_REMINDERS = 4

# Confirming an address is one tap, so it is chased sooner and dropped sooner
# than a passport. Two messages and then silence: somebody who has ignored both
# either cannot reach that inbox or does not want to, and a third would only
# teach them to filter us.
EMAIL_SCHEDULE = {0: timedelta(hours=24), 1: timedelta(days=3)}
MAX_EMAIL_REMINDERS = 2

# What to say about each missing step. Written to be read on a phone, in the
# order the app asks for them.
STEP_WORDING = {
    "details": "your name and WhatsApp number",
    "photo": "a photo of your face",
    "id_document": "a photo of your passport or ID card",
}


class Command(BaseCommand):
    help = "Email customers who have not finished identity verification."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Say who would be emailed and send nothing.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        now = timezone.now()

        candidates = (
            Profile.objects.filter(
                Q(verification_state=Profile.UNVERIFIED)
                | Q(verification_state=Profile.REJECTED)
            )
            .filter(reminders_sent__lt=MAX_REMINDERS)
            .exclude(user__is_suspended=True)
            # Desk accounts are not customers. They have no transfers to make
            # and chasing them for a passport is noise in the staff inbox.
            .filter(user__admin_profile__isnull=True)
            .select_related("user")
        )

        sent = 0
        for profile in candidates:
            wait = SCHEDULE.get(profile.reminders_sent)
            if wait is None:
                continue

            since = profile.last_reminder_at or profile.created_at
            if now - since < wait:
                continue

            # Nothing missing and nothing rejected means they are simply
            # waiting on the desk, which is not their problem to fix.
            missing = profile.missing_steps
            if not missing and profile.verification_state != Profile.REJECTED:
                continue

            if dry_run:
                self.stdout.write(
                    f"  would remind {profile.user.email} "
                    f"(#{profile.reminders_sent + 1}, missing: {', '.join(missing) or 'nothing'})"
                )
                sent += 1
                continue

            self.remind(profile, missing)
            sent += 1

        verb = "would email" if dry_run else "emailed"
        self.stdout.write(self.style.SUCCESS(f"{verb} {sent} account(s)."))

        confirmed = self.chase_unconfirmed_emails(now, dry_run)
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {confirmed} account(s) about confirming their address."
        ))

    def chase_unconfirmed_emails(self, now, dry_run):
        """Nudge people who never opened the confirmation link.

        A confirmed address is where a receipt goes and where a reset link goes
        if somebody is ever locked out, so an account without one is one bad
        day away from being unreachable. Kept separate from the identity chase
        above: they ask for different things and stop at different points.
        """
        candidates = (
            Profile.objects.filter(user__email_verified_at__isnull=True)
            .filter(email_reminders_sent__lt=MAX_EMAIL_REMINDERS)
            .exclude(user__is_suspended=True)
            .filter(user__admin_profile__isnull=True)
            .select_related("user")
        )

        sent = 0
        for profile in candidates:
            wait = EMAIL_SCHEDULE.get(profile.email_reminders_sent)
            if wait is None:
                continue
            since = profile.last_email_reminder_at or profile.user.date_joined
            if now - since < wait:
                continue

            if dry_run:
                self.stdout.write(
                    f"  would ask {profile.user.email} to confirm their address "
                    f"(#{profile.email_reminders_sent + 1})"
                )
            else:
                self.remind_email(profile)
            sent += 1
        return sent

    def remind_email(self, profile):
        from django.conf import settings

        from nkenzapay.accounts.models import EmailToken
        from nkenzapay.accounts.views import issue_email_token, verify_link

        # A fresh link every time. The one from sign-up may have expired, and a
        # reminder carrying a dead link is worse than no reminder: somebody
        # taps it, is told it is invalid, and concludes the account is broken.
        token = issue_email_token(profile.user, EmailToken.PURPOSE_VERIFY)

        notifications.notify(
            profile.user, "account.verify_email",
            email_body=(
                "Your NkenzaPay account is open, but we have not been able to "
                "confirm your email address yet.\n\n"
                "It takes one tap, and it matters for two things: it is where "
                "your receipts go, and it is the only way we can get you back "
                "in if you are ever locked out."
            ),
            email_action={
                "label": "Confirm my email",
                "url": verify_link(token),
                "footnote": "This link works once and expires in 24 hours.",
            },
        )

        profile.email_reminders_sent += 1
        profile.last_email_reminder_at = timezone.now()
        profile.save(update_fields=["email_reminders_sent", "last_email_reminder_at"])

    def remind(self, profile, missing):
        from django.conf import settings

        if profile.verification_state == Profile.REJECTED:
            body = (
                "We were not able to approve the document you sent, so your "
                "account is still waiting. Here is what we said at the "
                "time:\n\n"
                f"{profile.verification_note}\n\n"
                "This happens often and it is easily sorted. Send another one "
                "whenever you are ready and we will look again straight away."
            )
            summary = "we could not approve your document"
        else:
            wanted = [STEP_WORDING[step] for step in missing if step in STEP_WORDING]
            body = (
                "Your NkenzaPay account is open and there is one step left "
                "before you can send or receive money.\n\n"
                f"We still need {_join(wanted)}. It takes a couple of minutes, "
                "and we usually check it the same day."
            )
            summary = "we still need " + _join(wanted)

        notifications.notify(
            profile.user, "account.verification_reminder",
            context={"detail": summary},
            email_body=body,
            email_action={
                "label": "Finish setting up",
                "url": f"{settings.SITE_URL}/onboarding",
                "footnote": (
                    "You will stop getting these once your account is approved."
                ),
            },
        )

        profile.reminders_sent += 1
        profile.last_reminder_at = timezone.now()
        profile.save(update_fields=["reminders_sent", "last_reminder_at"])


def _join(items):
    """"a, b and c" — the way somebody would say it out loud."""
    if not items:
        return "one more thing"
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"
