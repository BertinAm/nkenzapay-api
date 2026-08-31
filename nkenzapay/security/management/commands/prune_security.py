from django.core.management.base import BaseCommand

from nkenzapay.security.tasks import prune_security_data


class Command(BaseCommand):
    help = "Delete expired security events, idempotency keys and blocks."

    def handle(self, *args, **options):
        result = prune_security_data()
        self.stdout.write(self.style.SUCCESS(
            f"Pruned {result['events']} events, {result['keys']} keys, "
            f"{result['blocks']} expired blocks."
        ))
