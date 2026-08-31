from django.db import models


class AuditEntry(models.Model):
    """Append-only. The application role has UPDATE and DELETE revoked on this
    table in production (see the SQL in nkenzapay/audit/sql/), and no admin
    screen exposes an edit path. The summary is stored display-ready so the log
    reads the same in five years as it did the day it was written."""

    actor = models.ForeignKey("accounts.User", null=True, blank=True,
                              on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=60, db_index=True)
    summary = models.CharField(max_length=280)
    target_type = models.CharField(max_length=40, blank=True)
    target_id = models.CharField(max_length=40, blank=True, db_index=True)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-at"]
        verbose_name_plural = "audit entries"

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} {self.action}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise RuntimeError("Audit entries cannot be modified.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("Audit entries cannot be deleted.")
