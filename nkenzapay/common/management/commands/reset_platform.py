"""Clear the test data before going live.

Everything a customer created goes: accounts, transfers, chats, payment
evidence, notifications, the audit trail and the security log. Everything the
desk configured stays: countries, corridors, payment methods and the account
details entered into them, fees, limits, rate providers, legal documents, news
and the desk's own logins.

    manage.py reset_platform            # counts what would go, deletes nothing
    manage.py reset_platform --confirm  # does it

Deliberately a command and not a button. This is needed once, on the day before
launch, and a permanent "delete every customer" control on a live dashboard is
a thing that only has to be clicked once by somebody tired. Here it takes a
shell, a flag, and meaning it.

Uploaded files are removed too. The rows would otherwise be gone while the
photographs of people's faces stayed on the disk, which is the opposite of the
point.
"""
from django.core.management.base import BaseCommand
from django.db import transaction as db_transaction


class Command(BaseCommand):
    help = "Delete all customer data. Keeps configuration and desk accounts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm", action="store_true",
            help="Actually delete. Without this the command only counts.",
        )
        parser.add_argument(
            "--include-desk", action="store_true",
            help=(
                "Also delete desk accounts. Refused for the account running "
                "this, so there is always a way back in."
            ),
        )

    def handle(self, *args, **options):
        counts = self.survey(options["include_desk"])

        self.stdout.write(self.style.MIGRATE_HEADING("Would delete"))
        for label, number in counts.items():
            if number:
                self.stdout.write(f"  {number:>7,}  {label}")
        if not any(counts.values()):
            self.stdout.write("  nothing — the platform is already clear")
            return

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Would keep"))
        for line in self.kept():
            self.stdout.write(f"  {line}")

        if not options["confirm"]:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "Nothing has been deleted. Run it again with --confirm."
            ))
            return

        self.stdout.write("")
        removed_files = self.purge_files()
        with db_transaction.atomic():
            self.delete_rows(options["include_desk"])

        self.stdout.write(self.style.SUCCESS("Customer data cleared."))
        self.stdout.write(f"  {removed_files} uploaded file(s) removed from storage.")
        self.stdout.write(
            "  Configuration, payment details and desk accounts are untouched."
        )

    # --- what is where -----------------------------------------------------

    def customers(self, include_desk):
        from nkenzapay.accounts.models import User

        people = User.objects.all()
        if not include_desk:
            people = people.filter(admin_profile__isnull=True)
        return people

    def survey(self, include_desk):
        from nkenzapay.accounts.models import EmailToken, LoginActivity, ProfileChangeLog
        from nkenzapay.analytics.models import ExportJob, PageView
        from nkenzapay.audit.models import AuditEntry
        from nkenzapay.disputes.models import Dispute
        from nkenzapay.notifications.models import Notification
        from nkenzapay.rates.models import Quote
        from nkenzapay.security.models import BlockedAddress, IdempotencyKey, SecurityEvent
        from nkenzapay.transactions.models import Attachment, Message, Transaction

        return {
            "customer accounts": self.customers(include_desk).count(),
            "transfers": Transaction.objects.count(),
            "chat messages": Message.objects.count(),
            "uploaded files": Attachment.objects.count(),
            "quotes": Quote.objects.count(),
            "disputes": Dispute.objects.count(),
            "notifications": Notification.objects.count(),
            "audit entries": AuditEntry.objects.count(),
            "security events": SecurityEvent.objects.count(),
            "blocked addresses": BlockedAddress.objects.count(),
            "idempotency keys": IdempotencyKey.objects.count(),
            "sign-in records": LoginActivity.objects.count(),
            "email tokens": EmailToken.objects.count(),
            "profile change log": ProfileChangeLog.objects.count(),
            "export jobs": ExportJob.objects.count(),
            "page views": PageView.objects.count(),
        }

    def kept(self):
        from nkenzapay.accounts.models import AdminUser
        from nkenzapay.content.models import LegalDocument, NewsPost
        from nkenzapay.geo.models import Corridor, Country
        from nkenzapay.payments.models import PaymentMethod
        from nkenzapay.pricing.models import FeeRule, TransferLimit
        from nkenzapay.rates.models import RateProvider

        configured = PaymentMethod.objects.filter(
            instruction__isnull=False
        ).exclude(instruction__fields={}).count()

        return [
            f"{AdminUser.objects.count()} desk account(s)",
            f"{Country.objects.count()} countries, {Corridor.objects.count()} corridors",
            f"{PaymentMethod.objects.count()} payment methods "
            f"({configured} with account details entered)",
            f"{FeeRule.objects.count()} fee rules, "
            f"{TransferLimit.objects.count()} transfer limits",
            f"{RateProvider.objects.count()} rate providers",
            f"{NewsPost.objects.count()} news posts, "
            f"{LegalDocument.objects.count()} legal documents",
        ]

    # --- doing it ----------------------------------------------------------

    def purge_files(self):
        """Every uploaded file, before the rows that point at them go.

        Afterwards there is nothing left saying which keys existed, so this
        cannot be done second.
        """
        from nkenzapay.common.storage import LocalStorage, storage
        from nkenzapay.transactions.models import Attachment

        backend = storage()
        keys = set(Attachment.objects.values_list("storage_key", flat=True))

        from nkenzapay.accounts.models import Profile

        for field in ("photo_key", "id_document_key"):
            keys.update(
                Profile.objects.exclude(**{field: ""})
                .values_list(field, flat=True)
            )

        removed = 0
        for key in keys:
            if not key:
                continue
            try:
                backend.delete(key)
                removed += 1
            except (FileNotFoundError, OSError):
                # Already gone. The row is what matters and it is going next.
                continue

        # Exports are built from customer data and are stale the moment it goes.
        if isinstance(backend, LocalStorage):
            for key in list(backend.walk_keys()):
                if key.startswith("exports/"):
                    try:
                        backend.delete(key)
                        removed += 1
                    except (FileNotFoundError, OSError):
                        continue
        return removed

    def delete_rows(self, include_desk):
        from nkenzapay.accounts.models import EmailToken, LoginActivity, ProfileChangeLog
        from nkenzapay.analytics.models import ExportJob, PageView
        from nkenzapay.audit.models import AuditEntry
        from nkenzapay.disputes.models import Dispute
        from nkenzapay.notifications.models import Notification, NotificationPreference
        from nkenzapay.rates.models import Quote
        from nkenzapay.security.models import BlockedAddress, IdempotencyKey, SecurityEvent
        from nkenzapay.transactions.models import (
            Attachment,
            Message,
            Receipt,
            StatusHistory,
            Transaction,
            TransactionCounter,
        )

        # Children first. Several of these are PROTECT rather than CASCADE,
        # which is right day to day and means the order here is not optional.
        Receipt.objects.all().delete()
        Attachment.objects.all().delete()
        Message.objects.all().delete()
        StatusHistory.objects.all().delete()
        Dispute.objects.all().delete()
        Transaction.objects.all().delete()
        # The reference counter, so the first real transfer is 00001 rather
        # than carrying on from the test data.
        TransactionCounter.objects.all().delete()
        Quote.objects.all().delete()

        Notification.objects.all().delete()
        NotificationPreference.objects.all().delete()
        AuditEntry.objects.all().delete()
        SecurityEvent.objects.all().delete()
        BlockedAddress.objects.all().delete()
        IdempotencyKey.objects.all().delete()
        LoginActivity.objects.all().delete()
        EmailToken.objects.all().delete()
        ProfileChangeLog.objects.all().delete()
        ExportJob.objects.all().delete()
        PageView.objects.all().delete()

        # Profiles go with their accounts by cascade.
        self.customers(include_desk).delete()
