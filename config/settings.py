"""Django settings for NkenzaPay.

Everything commercial (fees, limits, payment details, corridors) lives in the
database, not here. See nkenzapay/pricing and nkenzapay/payments.
"""
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    CORS_ALLOWED_ORIGINS=(list, ["http://localhost:3000"]),
    CSRF_TRUSTED_ORIGINS=(list, ["http://localhost:3000"]),
    DATABASE_URL=(str, "sqlite:///" + str(BASE_DIR / "db.sqlite3")),
    REDIS_URL=(str, ""),
    FX_PROVIDER=(str, "mock"),
    FX_API_KEY=(str, ""),
    FX_API_ACCOUNT_ID=(str, ""),
    EMAIL_BACKEND=(str, "django.core.mail.backends.console.EmailBackend"),
    MEDIA_STORAGE=(str, "local"),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="dev-only-key-replace-in-production")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "corsheaders",
    "django_filters",
    "django_otp",
    "django_otp.plugins.otp_totp",
    # NkenzaPay
    "nkenzapay.accounts",
    "nkenzapay.geo",
    "nkenzapay.rates",
    "nkenzapay.pricing",
    "nkenzapay.payments",
    "nkenzapay.transactions",
    "nkenzapay.disputes",
    "nkenzapay.content",
    "nkenzapay.notifications",
    "nkenzapay.analytics",
    "nkenzapay.audit",
    "nkenzapay.security",
    # No models. It is an app so that its deployment checks and management
    # commands are registered. See nkenzapay/common/checks.py.
    "nkenzapay.common",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Blocks refused addresses and records probes before anything else spends
    # work on the request.
    "nkenzapay.security.middleware.SecurityMiddleware",
    # Serves /static/ from STATIC_ROOT. Under Passenger every request reaches
    # Django, so without this the Django admin and the browsable API load
    # without any of their CSS. Placed after the blocklist so a refused address
    # gets nothing at all.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "nkenzapay.analytics.middleware.PageViewMiddleware",
]

# Which headers may be trusted for the caller's address.
#
# Only set these to headers a proxy you control actually writes. Anything else
# is client-supplied and lets an attacker forge their way past a block. Behind
# Cloudflare, CF-Connecting-IP is the right answer; direct-to-origin, leave the
# list empty so REMOTE_ADDR is used.
TRUSTED_IP_HEADERS = env.list("TRUSTED_IP_HEADERS", default=[])

# The front end proxies /api to this service, so behind Cloudflare the caller
# of record is the Worker and CF-Connecting-IP holds the Worker's address, not
# the customer's. Every visitor would then share one bucket: one attacker's
# failed logins would rate-limit everybody, and an automatic block would take
# the whole site off the air.
#
# The Worker therefore forwards the real address in X-Client-IP, and proves it
# is the Worker with this shared secret. Without the proof the header is
# ignored, because anything a client can set is something a client can forge.
# Empty means no proxy: the header is never believed.
PROXY_SHARED_SECRET = env("PROXY_SHARED_SECRET", default="")

# How many hostile events from one address before it is refused, as a JSON
# object of {event kind: hits}. The defaults live in the code and are therefore
# public; set this to numbers of your own so an attacker reading the source
# cannot work out how to stay underneath them.
#   SECURITY_THRESHOLDS={"login_failed": 12, "scanner": 8}
SECURITY_THRESHOLDS = env.json("SECURITY_THRESHOLDS", default={})

ROOT_URLCONF = "config.urls"

# Every API path is written without a trailing slash. Leaving APPEND_SLASH on
# makes Django redirect the slashed form, which collides with the front end's
# own trailing-slash redirect and produces a loop rather than a response.
APPEND_SLASH = False
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {"default": env.db()}
DATABASES["default"]["ATOMIC_REQUESTS"] = False
# Shared hosting hands out one connection pool and recycles processes often.
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=60)

if DATABASES["default"]["ENGINE"].endswith("mysql"):
    # MySQL needs telling. Without STRICT_TRANS_TABLES it silently truncates
    # an over-long field rather than refusing it, and a silently truncated
    # transaction reference is a support case nobody can explain.
    DATABASES["default"].setdefault("OPTIONS", {})
    DATABASES["default"]["OPTIONS"].update({
        "charset": "utf8mb4",
        "init_command": "SET sql_mode='STRICT_TRANS_TABLES', innodb_strict_mode=1",
    })

AUTH_USER_MODEL = "accounts.User"

# Argon2 first, per the security checklist.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # Compressed, but not hashed. The manifest backend refuses to serve a file
    # missing from the manifest, which turns a forgotten collectstatic into a
    # 500 rather than a missing stylesheet.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
# Where uploaded files land. Deliberately settable, because on cPanel the
# application directory *is* the document root, so the default below would sit
# one web-server misconfiguration away from being public. Point it at a sibling
# of the application directory in production. `manage.py check --deploy` fails
# if the path looks web-served.
MEDIA_ROOT = env("MEDIA_ROOT", default=str(BASE_DIR / "private-media"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_PAGINATION_CLASS": "nkenzapay.common.pagination.StandardPagination",
    "PAGE_SIZE": 25,
    "EXCEPTION_HANDLER": "nkenzapay.common.exceptions.api_exception_handler",
    "DEFAULT_THROTTLE_CLASSES": [
        # Fail open on a cache fault and record every refusal. See
        # nkenzapay/common/throttling.py.
        "nkenzapay.common.throttling.ScopedRate",
        "nkenzapay.common.throttling.AnonRate",
        "nkenzapay.common.throttling.UserRate",
    ],
    "DEFAULT_THROTTLE_RATES": {
        # Pricing is cheap and customers type; ordering is not.
        "quote": "120/min",
        "auth": "10/min",
        "register": "5/hour",
        "password_reset": "5/hour",
        "upload": "30/min",
        "order": "20/hour",
        "message": "60/min",
        "support": "10/hour",
        "anon": "300/min",
        "user": "600/min",
    },
}

# Sessions and CSRF. The Next.js app is same-site in production and forwards
# the CSRF token on every unsafe request.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 14
SESSION_COOKIE_NAME = "nkenzapay_session"
CSRF_COOKIE_NAME = "csrftoken"
# The front end reads this to put the token in a header, so it cannot be
# HttpOnly. Django validates the header against the session either way.
CSRF_COOKIE_HTTPONLY = False
CSRF_USE_SESSIONS = False
CSRF_FAILURE_VIEW = "nkenzapay.security.views.csrf_failure"
ADMIN_IDLE_TIMEOUT_SECONDS = 30 * 60

# The front end and the API live on two hosts. When they share a registrable
# domain (nkenzapay.com and api.nkenzapay.com) they are still same-site, so a
# Lax cookie is sent and nothing needs relaxing. Setting a cookie domain is
# what makes that work; leave it unset for single-host deployments.
SESSION_COOKIE_DOMAIN = env("COOKIE_DOMAIN", default=None) or None
CSRF_COOKIE_DOMAIN = SESSION_COOKIE_DOMAIN

CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_CREDENTIALS = True

SECURE_HSTS_SECONDS = 0 if DEBUG else 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = not DEBUG
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# The API answers JSON, never HTML a browser will execute, so the policy can be
# as tight as it goes. The front end sets its own.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)

# Only the headers the front end actually sends.
CORS_ALLOW_HEADERS = [
    "accept",
    "authorization",
    "content-type",
    "idempotency-key",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
]
CORS_ALLOW_METHODS = ["DELETE", "GET", "OPTIONS", "PATCH", "POST", "PUT"]

# Uploads are streamed to storage, so nothing legitimate is this large. The cap
# stops a request body being used to exhaust memory before a view sees it.
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 200

EMAIL_BACKEND = env("EMAIL_BACKEND")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="NkenzaPay <no-reply@nkenzapay.com>")

# Foreign exchange. The key is read here and never leaves the server.
FX = {
    "PROVIDER": env("FX_PROVIDER"),
    "API_KEY": env("FX_API_KEY"),
    "ACCOUNT_ID": env("FX_API_ACCOUNT_ID"),
}

# Uploads. Enforced in nkenzapay.transactions.uploads.
UPLOAD_LIMITS = {
    "image": 10 * 1024 * 1024,
    "document": 10 * 1024 * 1024,
    "video": 50 * 1024 * 1024,
}
UPLOAD_ALLOWED_TYPES = {
    "image/jpeg": ("image", [".jpg", ".jpeg"]),
    "image/png": ("image", [".png"]),
    "image/webp": ("image", [".webp"]),
    "application/pdf": ("document", [".pdf"]),
    "video/mp4": ("video", [".mp4"]),
}
SIGNED_URL_TTL_SECONDS = 60

# Encryption for files kept on disk.
#
# The local backend seals every file with AES-256-GCM before writing it, so a
# copy of the disk without this key is a directory full of noise. That is what
# makes shared hosting acceptable for payment evidence and photographs of
# customers: the host's staff, their backup system and anything else running on
# the account all see ciphertext.
#
# Generate one with `manage.py generate_media_key`. Keep it out of the
# repository and out of any backup that also holds the files.
#
# MEDIA_ENCRYPTION_KEYS is the rotation form, newest first:
#   MEDIA_ENCRYPTION_KEYS=k2:<base64>,k1:<base64>
# New writes use the first; the rest stay so old files still open. Run
# `manage.py encrypt_media` to rewrite everything under the first key, then
# drop the old one.
MEDIA_ENCRYPTION_KEY = env("MEDIA_ENCRYPTION_KEY", default="")
MEDIA_ENCRYPTION_KEYS = env.list("MEDIA_ENCRYPTION_KEYS", default=[])

# How long a closed transfer's attachments are kept. The strongest control on
# a shared disk is holding less: what was deleted last month cannot be read
# next month. 0 keeps everything, which is the default because a retention
# policy is a business decision, not a technical one. Swept by
# `manage.py sweep_media`, which the deployment runs nightly.
MEDIA_RETENTION_DAYS = env.int("MEDIA_RETENTION_DAYS", default=0)

# An upload URL is handed out, the browser PUTs the file, and the commit call
# attaches it. A customer who closes the tab in between leaves bytes on disk
# that no row points at — unreferenced, and never validated, because validation
# happens at commit. The sweep deletes them after this many hours.
UPLOAD_ORPHAN_HOURS = env.int("UPLOAD_ORPHAN_HOURS", default=24)

# Realtime and background work both need processes shared hosting does not
# offer. Both degrade rather than break: the chat falls back to polling, and
# Celery tasks run inline. See DEPLOYMENT.md.
REDIS_URL = env("REDIS_URL")
if REDIS_URL:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [REDIS_URL]},
        }
    }
    CELERY_BROKER_URL = REDIS_URL
    CELERY_RESULT_BACKEND = REDIS_URL
else:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    CELERY_TASK_ALWAYS_EAGER = True

CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = "UTC"

# Brute force lockout, per the security checklist: 5 failures, 15 minutes.
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60

# Caching. Redis where it exists; otherwise the database, because the
# per-process memory cache would give each worker its own idea of who is
# rate-limited and who is blocked.
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": "nkenzapay_cache",
        }
    }

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{levelname} {asctime} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "nkenzapay": {"handlers": ["console"], "level": "DEBUG" if DEBUG else "INFO",
                      "propagate": False},
    },
}
