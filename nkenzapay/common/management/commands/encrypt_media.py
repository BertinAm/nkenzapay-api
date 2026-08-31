"""Seal everything already on disk under the active key.

Two occasions call for this. Encryption was turned on for a deployment that had
already been running, so there are plaintext files sitting there; or a key was
rotated and the old one needs to stop being necessary.

Safe to run repeatedly. A file already sealed under the active key is skipped,
so nothing is rewritten for the sake of it.
"""
from django.core.management.base import BaseCommand, CommandError

from nkenzapay.common import crypto
from nkenzapay.common.storage import LocalStorage, storage


class Command(BaseCommand):
    help = "Encrypt files on disk, or re-encrypt them under the newest key."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would change and write nothing.",
        )

    def handle(self, *args, **options):
        backend = storage()
        if not isinstance(backend, LocalStorage):
            raise CommandError(
                "This only applies to the local backend. Object storage "
                "encrypts at rest on its own."
            )
        if not crypto.is_configured():
            raise CommandError(
                "MEDIA_ENCRYPTION_KEY is not set, so there is nothing to "
                "encrypt with. Generate one with: manage.py generate_media_key"
            )

        active, _ = crypto.active_key()
        dry_run = options["dry_run"]
        sealed = skipped = failed = 0

        for key in backend.walk_keys():
            try:
                raw = backend.read_raw(key)
            except OSError as exc:
                self.stderr.write(f"  could not read {key}: {exc}")
                failed += 1
                continue

            if crypto.looks_sealed(raw) and _key_id(raw) == active:
                skipped += 1
                continue

            try:
                plaintext = crypto.open_sealed(raw, aad=key)
            except crypto.DecryptionError as exc:
                # Sealed with a key this deployment no longer holds. Leave it
                # alone and say so: deleting somebody's payment evidence
                # because a key went missing is the worse outcome.
                self.stderr.write(self.style.ERROR(f"  {key}: {exc}"))
                failed += 1
                continue

            if dry_run:
                self.stdout.write(f"  would seal {key}")
            else:
                backend.save_bytes(key, plaintext)
            sealed += 1

        verb = "would be sealed" if dry_run else "sealed"
        self.stdout.write(self.style.SUCCESS(
            f"{sealed} {verb}, {skipped} already current, {failed} could not be read."
        ))
        if failed:
            raise CommandError(
                "Some files could not be opened. Add the old key back to "
                "MEDIA_ENCRYPTION_KEYS and run this again."
            )


def _key_id(raw: bytes) -> str:
    """Which key a sealed file belongs to, read from its header."""
    length = raw[5]
    return raw[6:6 + length].decode(errors="replace")
