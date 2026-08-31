"""Private file storage.

Nothing uploaded here is ever publicly readable. Two backends implement the
same three calls: a local one for development that writes under a directory
Django does not serve, and an S3-compatible one for production that hands out
short-lived signed URLs.

Both are addressed by an opaque key. The original filename is metadata on the
Attachment row, never part of the path — a file called `invoice.pdf.exe` should
not be able to name anything on disk.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import mimetypes
import os
import secrets
import time
from pathlib import Path

from django.conf import settings
from django.core.signing import BadSignature, TimestampSigner

from . import crypto

logger = logging.getLogger(__name__)


class StorageBackend:
    def presign_put(self, key, content_type, max_bytes):
        raise NotImplementedError

    def presign_get(self, key, ttl=None):
        raise NotImplementedError

    def save_bytes(self, key, data, content_type=""):
        raise NotImplementedError

    def read_bytes(self, key):
        raise NotImplementedError

    def delete(self, key):
        raise NotImplementedError


class LocalStorage(StorageBackend):
    """The disk.

    Used in development, and in production wherever object storage is not on
    the table. Shared hosting is the awkward case: the disk under the account
    is read by the host's staff, copied by their backup system, and reachable
    by any file-read bug in anything else running on the same account.

    Three things make that survivable.

    * **Nothing readable lands on it.** Bytes are sealed with AES-GCM before
      the write, keyed from the environment. See nkenzapay/common/crypto.py.
    * **Nothing on it is reachable by URL.** MEDIA_ROOT sits outside the
      document root, and the directory carries an Apache deny for the day
      somebody moves it.
    * **Only this account can read it.** Files are created 0600 inside a 0700
      tree, so another tenant who escapes their own directory still finds a
      permission error.

    Reads go through a Django view that checks who is asking.
    """

    # Dropped into MEDIA_ROOT so that a directory which somehow ends up inside
    # a document root is still refused by Apache. Belt and braces: the path
    # should not be web-served in the first place, and `manage.py check
    # --deploy` fails if it looks like it is.
    HTACCESS = """# NkenzaPay private media.
#
# Payment evidence and photographs of customers. Nothing here is ever served
# directly; every read goes through the application, which checks who is
# asking. If this file is doing any work, the media root is in the wrong place.
Require all denied

<IfModule !mod_authz_core.c>
  Order allow,deny
  Deny from all
</IfModule>
"""

    def __init__(self, root=None):
        self.root = Path(root or settings.MEDIA_ROOT)
        self.signer = TimestampSigner(salt="nkenzapay.storage")
        self._prepared = False

    def prepare_root(self):
        """Create the tree private, once per process."""
        if self._prepared:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        _restrict(self.root, 0o700)

        guard = self.root / ".htaccess"
        if not guard.exists():
            try:
                guard.write_text(self.HTACCESS, encoding="utf-8")
            except OSError:  # noqa: PERF203 - a read-only root is not fatal
                logger.warning("Could not write the media root's .htaccess.")
        self._prepared = True

    def path_for(self, key):
        # Keys are generated here and contain no user input, but a traversal
        # check costs nothing and closes the case for good.
        target = (self.root / key).resolve()
        if not str(target).startswith(str(self.root.resolve())):
            raise ValueError("Refusing a storage key that escapes the media root.")
        return target

    def presign_put(self, key, content_type, max_bytes):
        return {
            "method": "PUT",
            "url": f"/api/v1/uploads/local/{self.signer.sign(key)}",
            "headers": {"Content-Type": content_type},
            "key": key,
            "max_bytes": max_bytes,
            "direct": False,
        }

    def presign_get(self, key, ttl=None):
        return f"/api/v1/uploads/local/{self.signer.sign(key)}"

    def verify_signed_key(self, signed, ttl=None):
        ttl = ttl or settings.SIGNED_URL_TTL_SECONDS
        try:
            return self.signer.unsign(signed, max_age=ttl)
        except BadSignature:
            return None

    def save_bytes(self, key, data, content_type=""):
        self.prepare_root()
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        _restrict(path.parent, 0o700)

        if crypto.is_configured():
            data = crypto.seal(data, aad=key)
        else:
            _warn_unencrypted()

        # Written to a temporary name and moved into place, so a reader never
        # sees a half-written file, and created 0600 rather than chmod-ed after
        # the fact, which would leave a window where it was readable.
        temporary = path.with_name(f".{path.name}.part")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        os.replace(temporary, path)
        _restrict(path, 0o600)
        return key

    def read_bytes(self, key):
        data = self.path_for(key).read_bytes()
        return crypto.open_sealed(data, aad=key)

    def read_raw(self, key):
        """The bytes exactly as they sit on disk. For the re-sealing command."""
        return self.path_for(key).read_bytes()

    def delete(self, key):
        path = self.path_for(key)
        if path.exists():
            path.unlink()

    def walk_keys(self):
        """Every key currently on disk. Used by the sweep and the re-seal."""
        root = self.root.resolve()
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.name == ".htaccess" or path.name.endswith(".part"):
                continue
            yield path.relative_to(root).as_posix()


def _restrict(path, mode):
    """Tighten permissions where the platform has them. A no-op on Windows."""
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):
        pass


_warned = False


def _warn_unencrypted():
    global _warned
    if not _warned:
        logger.warning(
            "MEDIA_ENCRYPTION_KEY is not set. Uploaded files are being written "
            "to disk in the clear. Fine for development; not for a real "
            "deployment. See: manage.py generate_media_key"
        )
        _warned = True


class S3Storage(StorageBackend):
    """Production. Private bucket, no public policy, signed URLs only."""

    def __init__(self):
        import boto3

        self.bucket = os.environ["MEDIA_BUCKET"]
        self.client = boto3.client(
            "s3",
            endpoint_url=os.environ.get("MEDIA_ENDPOINT") or None,
            region_name=os.environ.get("MEDIA_REGION", "auto"),
        )

    def presign_put(self, key, content_type, max_bytes):
        url = self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=300,
        )
        return {"method": "PUT", "url": url, "headers": {"Content-Type": content_type},
                "key": key, "max_bytes": max_bytes, "direct": True}

    def presign_get(self, key, ttl=None):
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=ttl or settings.SIGNED_URL_TTL_SECONDS,
        )

    def save_bytes(self, key, data, content_type=""):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data,
                               ContentType=content_type or "application/octet-stream")
        return key

    def read_bytes(self, key):
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)


_backend = None


def storage() -> StorageBackend:
    global _backend
    if _backend is None:
        _backend = S3Storage() if os.environ.get("MEDIA_STORAGE") == "s3" else LocalStorage()
    return _backend


def build_key(prefix: str, content_type: str) -> str:
    """An opaque name. No original filename, no user id in the path."""
    extension = mimetypes.guess_extension(content_type) or ".bin"
    if extension == ".jpe":
        extension = ".jpg"
    stamp = time.strftime("%Y/%m")
    return f"{prefix}/{stamp}/{secrets.token_hex(16)}{extension}"


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def data_url_to_bytes(data_url: str) -> tuple[bytes, str]:
    """Decode the still the camera screen produces.

    The capture screen posts a canvas export rather than a multipart file, so
    the data URL is unpacked here and then validated by the same rules as any
    other upload.
    """
    if not data_url.startswith("data:"):
        raise ValueError("Expected a data URL.")
    header, _, payload = data_url.partition(",")
    content_type = header[5:].split(";")[0] or "image/jpeg"
    return base64.b64decode(payload), content_type
