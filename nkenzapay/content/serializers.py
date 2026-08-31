import bleach
from rest_framework import serializers

from .models import LegalDocument, NewsPost

# The composer offers bold, italic, lists, links and blockquote. Anything else
# arriving in a body is stripped, whoever sent it.
ALLOWED_TAGS = ["p", "br", "strong", "em", "b", "i", "u", "ul", "ol", "li",
                "a", "blockquote", "h2", "h3", "code", "pre"]
ALLOWED_ATTRIBUTES = {"a": ["href", "title", "rel", "target"]}
ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


def sanitise(html: str) -> str:
    return bleach.clean(
        html or "",
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )


class NewsListSerializer(serializers.ModelSerializer):
    tag_label = serializers.CharField(read_only=True)
    cover_url = serializers.SerializerMethodField()

    class Meta:
        model = NewsPost
        fields = ["slug", "title", "tag", "tag_label", "excerpt", "cover_url",
                  "publish_at", "view_count"]

    def get_cover_url(self, obj):
        """A public path is returned unchanged; a storage key gets signed.

        Article covers are public marketing images and should be cacheable and
        indexable, so the seeded ones are ordinary static files. Covers uploaded
        through the composer land in private storage and are served through a
        short-lived signed URL like everything else there.
        """
        if not obj.cover_key:
            return None
        if obj.cover_key.startswith(("/", "http://", "https://")):
            return obj.cover_key

        from nkenzapay.common.storage import storage

        return storage().presign_get(obj.cover_key, ttl=3600)


class NewsDetailSerializer(NewsListSerializer):
    body_html = serializers.SerializerMethodField()
    author = serializers.SerializerMethodField()

    class Meta(NewsListSerializer.Meta):
        fields = NewsListSerializer.Meta.fields + ["body_html", "author", "updated_at"]

    def get_body_html(self, obj):
        # Sanitised on write and again on read. Cheap, and it covers rows that
        # predate a tightening of the allowlist.
        return sanitise(obj.body_html)

    def get_author(self, obj):
        return obj.author.display_name if obj.author else "The NkenzaPay desk"


class AdminNewsSerializer(serializers.ModelSerializer):
    class Meta:
        model = NewsPost
        fields = ["id", "slug", "title", "tag", "excerpt", "body_html", "body_source",
                  "cover_key", "is_published", "publish_at", "view_count",
                  "created_at", "updated_at"]
        read_only_fields = ["view_count", "created_at", "updated_at"]

    def validate_body_html(self, value):
        return sanitise(value)

    def validate_slug(self, value):
        from django.utils.text import slugify

        return slugify(value)[:200]


class LegalDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = LegalDocument
        fields = ["slug", "title", "body_html", "effective_from", "updated_at"]
