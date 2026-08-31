"""Money helpers.

Two rules run through the whole platform:

1. Amounts are Decimal. Never float, anywhere, for any reason.
2. A currency's minor units decide rounding. XAF has none, INR has two, and
   assuming two decimals everywhere is how a platform quietly loses money.
"""
from decimal import ROUND_HALF_UP, Decimal

# Fallback table, used when a Currency row is not to hand. The database is the
# source of truth; this only keeps pure functions callable without a query.
MINOR_UNITS = {"XAF": 0, "XOF": 0, "INR": 2, "NGN": 2, "GHS": 2, "USD": 2, "EUR": 2}


def minor_units(currency) -> int:
    """Accept a Currency instance or a code and return its decimal places."""
    if hasattr(currency, "minor_units"):
        return currency.minor_units
    return MINOR_UNITS.get(str(currency).upper(), 2)


def quantize(amount: Decimal, currency) -> Decimal:
    """Round to the currency's minor units, half up.

    Half up rather than banker's rounding: the customer-visible lines have to
    add up when read off the screen, and half-even makes 2.5 and 3.5 behave
    differently in a way nobody can defend at a support desk.
    """
    places = minor_units(currency)
    exponent = Decimal(1).scaleb(-places)
    return Decimal(amount).quantize(exponent, rounding=ROUND_HALF_UP)


def to_minor(amount: Decimal, currency) -> int:
    """Whole minor units, for storage in integer columns and for exports."""
    return int(quantize(amount, currency).scaleb(minor_units(currency)))


def from_minor(value: int, currency) -> Decimal:
    return quantize(Decimal(value).scaleb(-minor_units(currency)), currency)


def group_indian(value: str) -> str:
    """Lakh/crore grouping: 1234567 -> 12,34,567.

    INR figures on the platform read the way an Indian customer expects, which
    is not what a plain thousands separator produces.
    """
    negative = value.startswith("-")
    digits = value.lstrip("-")
    if len(digits) <= 3:
        return ("-" if negative else "") + digits
    head, tail = digits[:-3], digits[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ("-" if negative else "") + ",".join(parts + [tail])


def group_western(value: str) -> str:
    negative = value.startswith("-")
    digits = value.lstrip("-")
    parts = []
    while len(digits) > 3:
        parts.insert(0, digits[-3:])
        digits = digits[:-3]
    if digits:
        parts.insert(0, digits)
    return ("-" if negative else "") + ",".join(parts)


def format_amount(amount: Decimal, currency_code: str) -> str:
    """Grouped digits without a symbol. The symbol is a display decision."""
    code = str(currency_code).upper()
    places = MINOR_UNITS.get(code, 2)
    fixed = f"{quantize(Decimal(amount), code):.{places}f}"
    whole, _, fraction = fixed.partition(".")
    grouped = group_indian(whole) if code == "INR" else group_western(whole)
    return f"{grouped}.{fraction}" if fraction else grouped


def display_amount(amount: Decimal, currency_code: str) -> str:
    """What a customer reads: symbol placement follows the currency."""
    code = str(currency_code).upper()
    body = format_amount(amount, code)
    if code == "INR":
        return f"₹{body}"
    return f"{body} {code}"
