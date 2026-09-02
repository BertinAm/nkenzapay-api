"""Print a new SECRET_KEY.

Django ships no command for this, and the usual advice is a `python -c`
one-liner. cPanel's Python app screen runs script files only, so on shared
hosting that one-liner is not available and the key ends up being generated
somewhere else and pasted in — through a laptop, an email, a chat window.

A key that signs every session and every upload link should be born on the
machine that uses it and go nowhere else. Hence a command.
"""
from django.core.management.base import BaseCommand
from django.core.management.utils import get_random_secret_key


class Command(BaseCommand):
    help = "Generate a value for SECRET_KEY."

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS(get_random_secret_key()))
        self.stdout.write("")
        self.stdout.write("Put it in .env as:")
        self.stdout.write("  SECRET_KEY=<the line above>")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING(
            "It signs sessions and upload links. Changing it later signs every "
            "customer out and invalidates every link already handed out."
        ))
