"""Seed the platform with its opening configuration.

Every figure here is data, not a constant: 6%, 5,000 XAF and 1,000 INR are the
values the platform launches with, and the desk changes them from the admin
without anyone touching this file again.

Idempotent. Running it twice changes nothing.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from nkenzapay.accounts.models import AdminRole, AdminUser, User
from nkenzapay.content.models import LegalDocument, NewsPost
from nkenzapay.geo.models import Corridor, Country, Currency
from nkenzapay.notifications.models import DeliveryRule
from nkenzapay.payments.models import PaymentInstruction, PaymentMethod
from nkenzapay.pricing.models import FeeRule, PlatformSetting, TransferLimit
from nkenzapay.rates.models import RateProvider

CURRENCIES = [
    ("XAF", "Central African CFA franc", "FCFA", 0),
    ("INR", "Indian rupee", "₹", 2),
    ("NGN", "Nigerian naira", "₦", 2),
    ("GHS", "Ghanaian cedi", "GH₵", 2),
    ("XOF", "West African CFA franc", "CFA", 0),
]

COUNTRIES = [
    # iso2, name, currency, dial, flag, enabled, origin, destination, order
    ("CM", "Cameroon", "XAF", "+237", "🇨🇲", True, True, True, 1),
    ("IN", "India", "INR", "+91", "🇮🇳", True, True, True, 2),
    ("NG", "Nigeria", "NGN", "+234", "🇳🇬", False, True, True, 3),
    ("GH", "Ghana", "GHS", "+233", "🇬🇭", False, True, True, 4),
    ("CI", "Côte d'Ivoire", "XOF", "+225", "🇨🇮", False, True, True, 5),
    ("SN", "Senegal", "XOF", "+221", "🇸🇳", False, True, True, 6),
]

# The methods themselves, without any account details.
#
# Real collection accounts are never written here. This file is source code and
# may be read by anyone; the numbers customers are told to pay into belong in
# the database, entered by the desk on the Payment methods screen after the
# first deploy. Seeding placeholders means a fresh install is obviously
# unconfigured rather than quietly pointing somewhere wrong.
METHODS = [
    # slug, label, country, side, icon, note, enabled, order, instruction fields
    ("mtn_momo", "MTN Mobile Money", "CM", "collect", "smartphone", "Instant", True, 1,
     {"number": "", "account_name": ""}),
    ("orange_money", "Orange Money", "CM", "collect", "smartphone", "Instant", True, 2,
     {"number": "", "account_name": ""}),
    ("upi", "UPI", "IN", "collect", "qr_code_2", "Instant", True, 1,
     {"upi_id": "", "merchant_name": ""}),
    ("bank", "Bank transfer", "IN", "collect", "account_balance", "NEFT/RTGS", True, 2,
     {"account_holder": "", "bank": "", "account_number": "", "ifsc": "",
      "branch": ""}),
    ("imps", "IMPS", "IN", "collect", "bolt", "24/7", True, 3,
     {"account_holder": "", "bank": "", "account_number": "", "ifsc": "",
      "branch": ""}),
    ("cbdc", "Digital Rupee", "IN", "collect", "toll", "CBDC", False, 4,
     {"wallet_id": "", "wallet_name": ""}),
    ("cash", "Cash deposit", "IN", "collect", "payments", "CDP", True, 5,
     {"location": "", "contact": ""}),
    # Payout side, used by the desk rather than shown on the quote page.
    ("mtn_payout", "MTN Mobile Money", "CM", "payout", "smartphone", "Instant", True, 1, {}),
    ("upi_payout", "UPI", "IN", "payout", "qr_code_2", "Instant", True, 1, {}),
]

# Obvious placeholders for local work, so the chat has something to render.
# Applied only with --demo, and only to a method that has no details yet.
DEMO_INSTRUCTIONS = {
    "mtn_momo": {"number": "6 00 000 000", "account_name": "NkenzaPay (example)"},
    "orange_money": {"number": "6 00 000 001", "account_name": "NkenzaPay (example)"},
    "upi": {"upi_id": "example@upi", "merchant_name": "NkenzaPay (example)"},
    "bank": {"account_holder": "NkenzaPay (example)", "bank": "Example Bank",
             "account_number": "0000000000", "ifsc": "EXMP0000000",
             "branch": "Example branch"},
    "imps": {"account_holder": "NkenzaPay (example)", "bank": "Example Bank",
             "account_number": "0000000000", "ifsc": "EXMP0000000",
             "branch": "Example branch"},
    "cash": {"location": "Example desk, by appointment", "contact": "+00 000 000 0000"},
}

DELIVERY_RULES = [
    ("admin.proof_uploaded", "Payment proof uploaded", True, 1),
    ("admin.customer_paid", "Customer tapped I have paid", True, 2),
    ("admin.dispute_opened", "Problem reported", True, 3),
    ("admin.message_received", "New customer message", False, 4),
    ("admin.transfer_created", "Order created", False, 5),
    ("admin.new_device_login", "Admin sign-in from a new device", True, 6),
]

LEGAL = [
    ("terms", "Terms and conditions"),
    ("privacy", "Privacy policy"),
    ("refunds", "Refund and cancellation policy"),
    ("disputes", "Transaction and dispute policy"),
    ("cookies", "Cookie policy"),
    ("licensing", "Regulatory and licensing information"),
]


class Command(BaseCommand):
    help = "Seed currencies, countries, corridors, methods, fees and limits."

    def add_arguments(self, parser):
        parser.add_argument("--admin-email", default="", help="Create an owner account")
        parser.add_argument("--admin-password", default="")
        parser.add_argument("--demo", action="store_true",
                            help="Add sample news articles for local work")

    @transaction.atomic
    def handle(self, *args, **options):
        self.seed_currencies()
        self.seed_countries()
        corridors = self.seed_corridors()
        self.seed_rates()
        self.seed_fees()
        self.seed_limits(corridors)
        self.seed_methods(demo=options["demo"])
        self.seed_settings()
        self.seed_delivery_rules()
        self.seed_legal()

        if options["admin_email"]:
            self.seed_admin(options["admin_email"], options["admin_password"])
        if options["demo"]:
            self.seed_news()

        self.stdout.write(self.style.SUCCESS("Seed complete."))

    def seed_currencies(self):
        for code, name, symbol, minor in CURRENCIES:
            Currency.objects.update_or_create(
                code=code,
                defaults={"name": name, "symbol": symbol, "minor_units": minor},
            )
        self.stdout.write(f"  currencies: {Currency.objects.count()}")

    def seed_countries(self):
        for iso2, name, currency, dial, flag, enabled, origin, dest, order in COUNTRIES:
            Country.objects.update_or_create(
                iso2=iso2,
                defaults={
                    "name": name, "currency_id": currency, "dial_code": dial,
                    "flag_emoji": flag, "is_enabled": enabled, "is_origin": origin,
                    "is_destination": dest, "sort_order": order,
                },
            )
        self.stdout.write(f"  countries: {Country.objects.count()}")

    def seed_corridors(self):
        """Cameroon to India is Receive; India to Cameroon is Send. The rest
        are created disabled so a country can be opened without a migration."""
        pairs = [("CM", "IN", True), ("IN", "CM", True)]
        for source, _, _, _, _, _, _, _, _ in COUNTRIES:
            if source in ("CM", "IN"):
                continue
            pairs.append((source, "IN", False))
            pairs.append(("IN", source, False))

        corridors = {}
        for source, target, enabled in pairs:
            corridor, _ = Corridor.objects.update_or_create(
                source_id=source, target_id=target, defaults={"is_enabled": enabled}
            )
            corridors[(source, target)] = corridor
        self.stdout.write(f"  corridors: {Corridor.objects.count()}")
        return corridors

    def seed_rates(self):
        RateProvider.objects.update_or_create(
            slug="mock",
            defaults={"label": "Development rates", "is_active": True,
                      "refresh_seconds": 60, "hold_seconds": 60, "markup_bps": 0},
        )
        RateProvider.objects.update_or_create(
            slug="xe",
            defaults={"label": "XE Currency Data", "is_active": False,
                      "refresh_seconds": 60, "hold_seconds": 60, "markup_bps": 25},
        )
        self.stdout.write("  rate providers: mock (active), xe")

    def seed_fees(self):
        """One global rule at 6%. Country overrides go beside it, not instead."""
        FeeRule.objects.get_or_create(
            corridor=None, country=None, direction="", is_active=True,
            defaults={
                "percent": Decimal("6.00"),
                "min_fee": None, "max_fee": None,
                "fee_currency_id": "INR",
                "valid_from": timezone.now(),
            },
        )
        self.stdout.write("  fee rule: 6% global")

    def seed_limits(self, corridors):
        TransferLimit.objects.update_or_create(
            corridor=corridors[("CM", "IN")], direction="receive",
            defaults={
                "currency_id": "XAF",
                "minimum": Decimal("5000"),
                "maximum": Decimal("5000000"),
                "daily_maximum": Decimal("5000000"),
                "monthly_maximum": Decimal("20000000"),
                "manual_review_above": Decimal("900000"),
            },
        )
        TransferLimit.objects.update_or_create(
            corridor=corridors[("IN", "CM")], direction="send",
            defaults={
                "currency_id": "INR",
                "minimum": Decimal("1000"),
                "maximum": Decimal("500000"),
                "daily_maximum": Decimal("200000"),
                "monthly_maximum": Decimal("1000000"),
                "manual_review_above": Decimal("90000"),
            },
        )
        self.stdout.write("  limits: 5,000 XAF receive · 1,000 INR send")

    def seed_methods(self, demo=False):
        """Create the methods, never overwriting details the desk has entered.

        Re-running the seed after go-live must not blank the account numbers
        customers are told to pay into.
        """
        unconfigured = []

        for slug, label, country, side, icon, note, enabled, order, fields in METHODS:
            method, _ = PaymentMethod.objects.update_or_create(
                slug=slug,
                defaults={"label": label, "country_id": country, "side": side,
                          "icon": icon, "note": note, "is_enabled": enabled,
                          "sort_order": order},
            )
            if not fields:
                continue

            instruction, _created = PaymentInstruction.objects.get_or_create(
                method=method,
                defaults={"fields": fields, "reference_format": "NKP-{order}"},
            )

            if demo and not any(instruction.fields.values()):
                placeholder = DEMO_INSTRUCTIONS.get(slug)
                if placeholder:
                    instruction.fields = placeholder
                    instruction.save(update_fields=["fields"])

            if not any(instruction.fields.values()):
                unconfigured.append(label)

        self.stdout.write(f"  payment methods: {PaymentMethod.objects.count()}")
        if unconfigured:
            self.stdout.write(self.style.WARNING(
                "  No payment details set for: "
                + ", ".join(sorted(set(unconfigured)))
                + ". Enter them on the admin Payment methods screen before "
                "taking a real transfer."
            ))

    def seed_settings(self):
        for key, value in PlatformSetting.DEFAULTS.items():
            PlatformSetting.objects.get_or_create(key=key, defaults={"value": value})
        self.stdout.write("  platform settings")

    def seed_delivery_rules(self):
        for event, label, email, order in DELIVERY_RULES:
            DeliveryRule.objects.update_or_create(
                event=event,
                defaults={"label": label, "email_admins": email, "sort_order": order},
            )

    def seed_legal(self):
        for slug, title in LEGAL:
            LegalDocument.objects.get_or_create(
                slug=slug,
                defaults={"title": title,
                          "body_html": "<p>This policy is being prepared.</p>"},
            )

    def seed_admin(self, email, password):
        user, created = User.objects.get_or_create(
            email=email.lower(),
            defaults={"is_staff": True, "is_superuser": True,
                      "email_verified_at": timezone.now()},
        )
        if created and password:
            user.set_password(password)
            user.save()
        AdminUser.objects.update_or_create(
            user=user, defaults={"role": AdminRole.OWNER}
        )
        self.stdout.write(f"  owner account: {user.email}")

    def seed_news(self):
        """Six published articles with real cover photography.

        The covers are public paths under the front end's /news directory, not
        private storage keys — an article image is marketing, and should be
        cacheable and indexable like the rest of the page.
        """
        samples = [
            (
                "cameroon-india-corridor-live",
                "The Cameroon to India corridor is live",
                "milestone",
                "Students and workers in India can now receive money from home in "
                "minutes, and send it back the same way.",
                "<p>The first corridor is open. Money sent from Cameroon on MTN "
                "Mobile Money or Orange Money reaches an Indian account the same "
                "day, and the figure on screen is what arrives.</p>"
                "<p>Every transfer opens its own chat with the desk, so there is "
                "always someone to ask.</p>",
                14,
            ),
            (
                "install-from-your-browser",
                "Install NkenzaPay from your browser",
                "product",
                "Add it to your home screen and it opens like any other app, with "
                "no store to visit.",
                "<p>NkenzaPay installs straight from the browser. On Android, tap "
                "Install when the prompt appears. On iPhone, tap Share and then "
                "Add to Home Screen.</p>"
                "<p>Once it is on your home screen it opens full screen and "
                "remembers you between visits.</p>",
                21,
            ),
            (
                "nigeria-and-ghana-in-testing",
                "Nigeria and Ghana are in testing",
                "coming_soon",
                "Two more corridors are being tested with a small group of "
                "customers before they open.",
                "<p>Nigeria and Ghana are running with a handful of customers "
                "while the payout side is proven. Both will appear on the Receive "
                "and Send pages the moment they are ready.</p>",
                26,
            ),
            (
                "rates-refresh-every-60-seconds",
                "Rates now refresh every 60 seconds",
                "rates",
                "The rate you are shown is held for a minute while you decide, and "
                "stored with the transfer.",
                "<p>The quote on screen carries a countdown. While it runs, that "
                "rate is yours; when it expires you are asked for a fresh one "
                "rather than being re-priced without noticing.</p>"
                "<p>Whichever rate you accept is stored with the transfer and "
                "printed on the receipt.</p>",
                34,
            ),
            (
                "desk-pays-out-until-9pm",
                "The desk now pays out until 9pm",
                "desk",
                "Later hours on both sides of the corridor, so an evening transfer "
                "does not wait until morning.",
                "<p>The desk verifies payments and sends payouts until 9pm IST, "
                "seven days a week. Transfers created after that are handled first "
                "thing the next morning.</p>",
                48,
            ),
            (
                "digital-rupee-on-send",
                "Digital Rupee accepted on Send",
                "new_method",
                "Pay from a CBDC wallet and the instructions appear in your "
                "transfer chat, the same way UPI does.",
                "<p>Customers sending from India can now pay from a CBDC wallet. "
                "Pick Digital Rupee on the Send page and the wallet instructions "
                "appear in the transaction chat, the same way UPI works today.</p>",
                62,
            ),
        ]

        from datetime import timedelta

        now = timezone.now()
        for slug, title, tag, excerpt, body, days_ago in samples:
            NewsPost.objects.update_or_create(
                slug=slug,
                defaults={
                    "title": title,
                    "tag": tag,
                    "excerpt": excerpt,
                    "body_html": body,
                    "cover_key": f"/news/{slug}.webp",
                    "is_published": True,
                    "publish_at": now - timedelta(days=days_ago),
                },
            )
        self.stdout.write(f"  news posts: {NewsPost.objects.count()}")
