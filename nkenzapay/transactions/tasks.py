from celery import shared_task


@shared_task
def build_receipt_pdf(receipt_id):
    from .models import Receipt
    from .receipts import build_and_store

    receipt = Receipt.objects.filter(pk=receipt_id).first()
    if receipt is None:
        return None
    return build_and_store(receipt)
