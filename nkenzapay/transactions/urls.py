from django.urls import path

from . import views

urlpatterns = [
    path("<str:reference>", views.TransactionDetail.as_view(), name="transaction-detail"),
    path("<str:reference>/messages", views.MessageListCreate.as_view(),
         name="transaction-messages"),
    path("<str:reference>/messages/read", views.MarkThreadRead.as_view(),
         name="transaction-mark-read"),
    path("<str:reference>/attachments/upload-url", views.AttachmentUploadUrl.as_view(),
         name="attachment-upload-url"),
    path("<str:reference>/attachments", views.AttachmentCommit.as_view(),
         name="attachment-commit"),
    path("<str:reference>/actions/<str:action>", views.TransactionAction.as_view(),
         name="transaction-action"),
    path("<str:reference>/dispute", views.DisputeCreate.as_view(), name="transaction-dispute"),
    path("<str:reference>/receipt", views.ReceiptView.as_view(), name="transaction-receipt"),
    path("<str:reference>/receipt.pdf", views.ReceiptPdfView.as_view(),
         name="transaction-receipt-pdf"),
]
