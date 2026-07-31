"""Django settings for the Discern platform."""

import os
from decimal import Decimal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Read `.env` into the environment for anyone running without Docker.

    Docker passes these as real environment variables via `env_file`, so this
    is a no-op there — and deliberately so: **a real environment variable
    always wins**. Otherwise a stale `.env` in the working tree would silently
    override what the container was configured with.

    Without this, editing `.env` outside Docker does nothing at all, and the
    app goes looking for a host called `db` that only exists inside compose.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-not-for-production")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.accounts",
    "apps.core",
    "apps.platform_core",
    "apps.crm",
    "apps.sales",
    "apps.projects",
    "apps.engineering",
    "apps.procurement",
    "apps.inventory",
    "apps.fabrication",
    "apps.subcontracts",
    "apps.finance",
    "apps.reporting",
    "apps.ui",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "apps" / "ui" / "templates"],
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

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "discern"),
        "USER": os.environ.get("POSTGRES_USER", "postgres"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "ATOMIC_REQUESTS": False,
    }
}

AUTH_USER_MODEL = "accounts.AppUser"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "en-in"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = []

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Celery ---------------------------------------------------------------
# Redis is only needed for the background outbox drain. Nothing in a web
# request calls `.delay()`, so the application runs perfectly well with no
# Redis at all — you simply drain the outbox yourself (`manage.py drain_outbox`)
# instead of a worker doing it on a schedule. That makes Redis optional for
# anyone running this locally without Docker.
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_ALWAYS_EAGER = os.environ.get("CELERY_EAGER", "0") == "1"

# --- Business policy ------------------------------------------------------
# Set here rather than in code so an administrator can change them without a
# deploy. Everything is INR: Discern trades in a single currency today, so no
# currency is carried on amounts. See docs/PROGRESS.md for what multi-currency
# would cost when it is needed.
CURRENCY = "INR"

#: Above this, a purchase order or service order needs the final approver's
#: signature rather than the buyer's alone.
APPROVAL_THRESHOLD = Decimal(os.environ.get("APPROVAL_THRESHOLD", "500000"))

#: Discern's rule: every line quoted by more than two vendors. Below this
#: value a single trusted vendor is acceptable — a three-quote exercise on a
#: small purchase costs more than it saves. Set to 0 to require three quotes
#: at every value.
RFQ_MINIMUM_VENDORS = int(os.environ.get("RFQ_MINIMUM_VENDORS", "3"))
RFQ_MINIMUM_VENDORS_BELOW_VALUE = Decimal(
    os.environ.get("RFQ_MINIMUM_VENDORS_BELOW_VALUE", "0")
)

# --- Platform -------------------------------------------------------------
# Maximum delivery attempts before an outbox event is dead-lettered.
OUTBOX_MAX_ATTEMPTS = int(os.environ.get("OUTBOX_MAX_ATTEMPTS", "5"))
