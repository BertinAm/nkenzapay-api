from django.db import models
from django.utils import timezone


class NewsPost(models.Model):
    """Public announcements, written in the admin composer.

    body_html is what renders; body_source is the editor's own document, kept
    so an article can be reopened and edited without a lossy round trip through
    sanitised HTML.
    """

    TAGS = [
        ("milestone", "Milestone"),
        ("new_method", "New method"),
        ("coming_soon", "Coming soon"),
        ("product", "Product"),
        ("rates", "Rates"),
        ("desk", "Desk"),
        ("security", "Security"),
    ]

    slug = models.SlugField(unique=True, max_length=200)
    title = models.CharField(max_length=180)
    tag = models.CharField(max_length=30, choices=TAGS, default="product")
    excerpt = models.CharField(max_length=280, blank=True)
    body_html = models.TextField(blank=True)
    body_source = models.JSONField(default=dict, blank=True)
    cover_key = models.CharField(max_length=255, blank=True)
    is_published = models.BooleanField(default=False)
    publish_at = models.DateTimeField(null=True, blank=True)
    view_count = models.PositiveIntegerField(default=0)
    author = models.ForeignKey("accounts.User", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-publish_at", "-created_at"]

    def __str__(self):
        return self.title

    @property
    def is_live(self):
        return self.is_published and (self.publish_at is None or self.publish_at <= timezone.now())

    @property
    def tag_label(self):
        return dict(self.TAGS).get(self.tag, self.tag)


class NewsletterSubscriber(models.Model):
    """Double opt-in. Nothing goes out until confirmed_at is set, and every
    message carries the unsubscribe token."""

    email = models.EmailField(unique=True)
    user = models.ForeignKey("accounts.User", null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="+")
    confirm_token = models.CharField(max_length=64, db_index=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.email

    @property
    def is_active(self):
        return self.confirmed_at is not None and self.unsubscribed_at is None


class LegalDocument(models.Model):
    """Terms, privacy, refunds, disputes, cookies, licensing. Edited in the
    admin so a policy change does not need a release."""

    slug = models.SlugField(primary_key=True, max_length=60)
    title = models.CharField(max_length=140)
    body_html = models.TextField(blank=True)
    effective_from = models.DateField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title
