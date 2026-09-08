"""Print the tail of the application log.

cPanel offers a box that runs Python and no shell, so `tail` is not available
where it is most needed: on the deployment, at the moment something has just
returned a 500. This is tail, as a management command.

    python manage.py show_log
    python manage.py show_log --lines 200
    python manage.py show_log --grep Traceback

Reads the last chunk of the file rather than all of it, so a log that has grown
to hundreds of megabytes still prints in a moment.
"""
import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

# Where Passenger and Django are configured to write, in the order worth trying.
CANDIDATES = ["stderr.log", "logs/stderr.log", "passenger.log", "django.log"]

# Reading only the end. A traceback is a few kilobytes; the file may not be.
TAIL_BYTES = 512 * 1024


class Command(BaseCommand):
    help = "Print the end of the application log, for hosts without a shell."

    def add_arguments(self, parser):
        parser.add_argument("--lines", type=int, default=80)
        parser.add_argument("--path", default="", help="An explicit log file.")
        parser.add_argument(
            "--grep", default="", help="Only lines containing this text."
        )

    def handle(self, *args, **options):
        path = self.find(options["path"])
        if path is None:
            self.stdout.write(self.style.ERROR("No log file found."))
            self.stdout.write("  Looked beside BASE_DIR for: " + ", ".join(CANDIDATES))
            self.stdout.write("  Pass one with --path if it lives somewhere else.")
            return

        size = path.stat().st_size
        self.stdout.write(self.style.MIGRATE_HEADING(f"{path} ({size:,} bytes)"))

        lines = self.tail(path, options["lines"], options["grep"])
        if not lines:
            self.stdout.write("  Nothing matched.")
            return
        for line in lines:
            self.stdout.write("  " + line.rstrip())

    def find(self, explicit):
        if explicit:
            candidate = Path(explicit).expanduser()
            return candidate if candidate.is_file() else None

        base = Path(settings.BASE_DIR)
        # The log usually sits beside the application rather than inside it.
        for root in (base, base.parent):
            for name in CANDIDATES:
                candidate = root / name
                if candidate.is_file():
                    return candidate
        return None

    def tail(self, path, count, needle):
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - TAIL_BYTES))
            # errors="replace": a log is bytes somebody else wrote, and a broken
            # sequence in it must not stop us reading the traceback underneath.
            text = handle.read().decode("utf-8", errors="replace")

        lines = text.splitlines()
        if needle:
            lines = [line for line in lines if needle in line]
        return lines[-count:]
