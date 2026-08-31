"""Take things off the disk.

The strongest control available on a disk somebody else owns is holding less on
it. Encryption protects what is there; this removes what does not need to be.

Two jobs, both safe to run every night:

**Orphans.** An upload URL is handed out, the browser PUTs the file, and a
second call attaches it. A customer who closes the tab in between leaves bytes
that no database row points at. Nothing ever reads them and nothing ever
deletes them, and — because validation happens at the attach step, not the
upload step — nobody has even checked what they are. They accumulate until
somebody notices.

**Retention.** Payment screenshots and identity photographs of a transfer that
closed months ago are a liability, not an asset. Past MEDIA_RETENTION_DAYS the
file goes and the row stays, so the thread still records that a document was
sent, by whom and when. Off by default: how long to keep records is a decision
about your own obligations, not one this code should make.

    manage.py sweep_media            # do it
    manage.py sweep_media --dry-run  # say what it would do
"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from nkenzapay.common.storage import LocalStorage, storage


class Command(BaseCommand):
    help = "Delete abandoned uploads and attachments past their retention date."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would be deleted and delete nothing.")
        parser.add_argument("--orphans-only", action="store_true",
                            help="Skip retention; only clear abandoned uploads.")

    def handle(self, *args, **options):
        backend = storage()
        if not isinstance(backend, LocalStorage):
            raise CommandError(
                "This sweeps the local disk. Object storage does the same job "
                "with a lifecycle rule."
            )

        self.dry_run = options["dry_run"]
        removed = self.sweep_orphans(backend)
        expired = 0 if options["orphans_only"] else self.sweep_expired(backend)

        verb = "would be removed" if self.dry_run else "removed"
        self.stdout.write(self.style.SUCCESS(
            f"{removed} abandoned uploads and {expired} expired attachments {verb}."
        ))

    # --- abandoned uploads ------------------------------------------------

    def sweep_orphans(self, backend):
        cutoff = timezone.now() - timedelta(hours=settings.UPLOAD_ORPHAN_HOURS)
        known = referenced_keys()
        removed = 0

        for key in backend.walk_keys():
            if key in known:
                continue

            path = backend.path_for(key)
            try:
                written = timezone.datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.get_current_timezone()
                )
            except OSError:
                continue

            # Young files are left alone: one may be mid-flight between the PUT
            # and the call that attaches it.
            if written > cutoff:
                continue

            if self.dry_run:
                self.stdout.write(f"  abandoned: {key}")
            else:
                backend.delete(key)
            removed += 1

        return removed

    # --- retention --------------------------------------------------------

    def sweep_expired(self, backend):
        days = settings.MEDIA_RETENTION_DAYS
        if not days:
            self.stdout.write(
                "MEDIA_RETENTION_DAYS is 0, so nothing expires. Attachments are "
                "kept for the life of the deployment."
            )
            return 0

        from nkenzapay.transactions.models import Attachment

        cutoff = timezone.now() - timedelta(days=days)
        expired = (
            Attachment.objects
            .filter(transaction__closed_at__lt=cutoff, purged_at__isnull=True)
            .exclude(storage_key="")
            .select_related("transaction")
        )

        count = 0
        for attachment in expired:
            if self.dry_run:
                self.stdout.write(
                    f"  expired: {attachment.original_name} "
                    f"on {attachment.transaction.reference}"
                )
            else:
                try:
                    backend.delete(attachment.storage_key)
                except OSError as exc:
                    self.stderr.write(f"  could not delete {attachment.storage_key}: {exc}")
                    continue
                # The row survives the file. The thread should still show that
                # a document was sent, by whom and when, after the document
                # itself is gone.
                attachment.storage_key = ""
                attachment.purged_at = timezone.now()
                attachment.save(update_fields=["storage_key", "purged_at"])
            count += 1

        return count


def referenced_keys() -> set[str]:
    """Every storage key any row points at.

    Anything on disk and not in here is unreachable by the application, which
    is what makes it safe to delete — so getting this wrong deletes a
    customer's payment evidence.

    A hand-written list of models would be exactly the wrong shape for that: it
    goes stale the first time somebody adds a model and forgets this file, and
    the symptom is silent deletion. So every text field on every model whose
    name ends in `_key` is collected instead. Over-collecting is free — the
    worst case is that an abandoned file survives another night — while
    under-collecting destroys something. When the two errors cost that
    differently, lean hard towards the cheap one.
    """
    from django.apps import apps
    from django.db import models

    keys: set[str] = set()

    for model in apps.get_models():
        fields = [
            field.name
            for field in model._meta.get_fields()
            if isinstance(field, models.CharField) and field.name.endswith("_key")
        ]
        for field in fields:
            keys.update(
                model.objects.exclude(**{field: ""})
                .exclude(**{f"{field}__isnull": True})
                .values_list(field, flat=True)
            )

    keys.discard("")
    keys.discard(None)
    return keys
