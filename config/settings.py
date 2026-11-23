"""Django settings for the Campaign Financial Management MVP.

Everything environment-specific is read from environment variables so the
same code runs locally (SQLite) and in production (PostgreSQL via DATABASE_URL).
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path):
    """Read KEY=value lines from a local .env file (no extra dependency). Real env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "*" if DEBUG else "").split(",") if h]
CSRF_TRUSTED_ORIGINS = [
    o for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o
]
# Render sets this automatically for web services.
if os.environ.get("RENDER_EXTERNAL_HOSTNAME"):
    ALLOWED_HOSTS.append(os.environ["RENDER_EXTERNAL_HOSTNAME"])
    CSRF_TRUSTED_ORIGINS.append(f"https://{os.environ['RENDER_EXTERNAL_HOSTNAME']}")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "finance",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "finance.context_processors.roles",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


def _database_from_url(url: str) -> dict:
    """Minimal postgres://user:pass@host:port/name parser (avoids an extra dependency)."""
    from urllib.parse import urlparse, unquote

    p = urlparse(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": p.path.lstrip("/"),
        "USER": unquote(p.username or ""),
        "PASSWORD": unquote(p.password or ""),
        "HOST": p.hostname or "",
        "PORT": str(p.port or ""),
        "CONN_MAX_AGE": 60,
    }


if os.environ.get("DATABASE_URL"):
    DATABASES = {"default": _database_from_url(os.environ["DATABASE_URL"])}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Asia/Colombo"
USE_I18N = True
USE_TZ = True
# Off on purpose: money is formatted by the lkr/num template filters; a global separator
# breaks numbers in form values and data attributes (e.g. "1,234" ids, JS parseFloat).
USE_THOUSAND_SEPARATOR = False

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
                    if not DEBUG else "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", BASE_DIR / "media"))

# Uploaded bills/receipts: keep them modest for phone uploads.
DATA_UPLOAD_MAX_MEMORY_SIZE = 15 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CURRENCY = "LKR"

# AI document reading – enabled when the chosen provider's API key is present.
#   AI_PROVIDER=openai    → GPT-6 Luna (cheap default)    needs OPENAI_API_KEY
#   AI_PROVIDER=anthropic → Claude                        needs ANTHROPIC_API_KEY
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_PROVIDER = os.environ.get("AI_PROVIDER") or ("anthropic" if ANTHROPIC_API_KEY and not OPENAI_API_KEY else "openai")
AI_MODEL = os.environ.get("AI_MODEL", "")  # blank = provider default (gpt-6-luna / claude-haiku-5-5)
AI_REASONING_EFFORT = os.environ.get("AI_REASONING_EFFORT", "low")  # OpenAI only: none/low/medium/high
# Query agent ("Ask" page) – LangGraph + OpenAI tool calling.
AGENT_MODEL = os.environ.get("AGENT_MODEL", "")  # blank = AI_MODEL or gpt-6-luna
AGENT_REASONING_EFFORT = os.environ.get("AGENT_REASONING_EFFORT", "low")
AGENT_USE_RESPONSES_API = os.environ.get("AGENT_USE_RESPONSES_API", "1") == "1"
AI_AUTO_READ = os.environ.get("AI_AUTO_READ", "1") == "1"  # read bills/receipts/quotes on upload

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SECURE_REDIRECT_EXEMPT = [r"^healthz$"]  # platform health checks may use plain HTTP
    SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "3600"))
