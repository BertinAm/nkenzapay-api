"""Print a new media encryption key."""
from django.core.management.base import BaseCommand

from nkenzapay.common import crypto


class Command(BaseCommand):
    help = "Generate a key for encrypting uploaded files at rest."

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS(crypto.generate_key()))
        self.stdout.write("")
        self.stdout.write("Put it in .env as:")
        self.stdout.write("  MEDIA_ENCRYPTION_KEY=<the line above>")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING(
            "Store a copy somewhere the file backups do not reach. A key kept "
            "beside what it protects protects nothing, and a key that is only "
            "in .env is gone the day that file is."
        ))
