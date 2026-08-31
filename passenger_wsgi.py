"""Entry point for cPanel's Python app support (Passenger).

Namecheap shared hosting runs Python apps under Phusion Passenger, which looks
for this file and expects a WSGI callable named `application`. Point the app's
"Application startup file" at this and its "Application root" at the directory
containing it.

Passenger runs whatever Python the cPanel virtualenv provides, so nothing here
may assume the project directory is on the path.
"""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Passenger starts the process from a directory that is not necessarily this
# one, so the project has to put itself on the path.
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
