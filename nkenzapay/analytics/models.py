from django.db import models


class PageView(models.Model):
    """One row per page a visitor opens.

    Recorded first-party. No third-party analytics script sits on a page where
    a customer is reading their own transfer figures.
    """

    MOBILE = "mobile"
    DESKTOP = "desktop"
    TABLET = "tablet"

    path = models.CharField(max_length=200, db_index=True)
    session_key = models.CharField(max_length=64, db_index=True)
    user = models.ForeignKey("accounts.User", null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="+")
    referrer = models.CharField(max_length=300, blank=True)
    source = models.CharField(max_length=40, blank=True)
    device = models.CharField(max_length=12, default=DESKTOP)
    country = models.CharField(max_length=2, blank=True)
    at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["path", "-at"])]

    def __str__(self):
        return f"{self.path} at {self.at:%Y-%m-%d %H:%M}"


class ExportJob(models.Model):
    """A queued export. Long ranges are built on a worker and handed back as a
    signed link rather than held open on a request."""

    QUEUED = "queued"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"

    requested_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    datasets = models.JSONField(default=list)
    filters = models.JSONField(default=dict, blank=True)
    date_from = models.DateField(null=True, blank=True)
    date_to = models.DateField(null=True, blank=True)
    fmt = models.CharField(max_length=8, default="csv")
    state = models.CharField(max_length=12, default=QUEUED)
    row_count = models.PositiveIntegerField(default=0)
    storage_key = models.CharField(max_length=255, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{', '.join(self.datasets)} ({self.state})"
