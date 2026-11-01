from pathlib import Path
from waviot_telegram.config import load_environment

values = load_environment()
BASE_DIR = Path(__file__).resolve().parent
BOT_DATABASE = Path(values.get('BOT_DATABASE', 'artifacts/runtime/state.sqlite3')).resolve()
SECRET_FILE = Path(values.get('ADMIN_SECRET_KEY_FILE', str(BOT_DATABASE.parent / 'admin-secret.key')))
SECRET_KEY = values.get('ADMIN_SECRET_KEY') or SECRET_FILE.read_text().strip()
DEBUG = False
PUBLIC = values.get('ADMIN_PUBLIC', 'false').lower() == 'true'
ALLOWED_HOSTS = [v.strip() for v in values.get('ADMIN_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1]').split(',') if v.strip()]
CSRF_TRUSTED_ORIGINS = [v.strip() for v in values.get('ADMIN_CSRF_ORIGINS', '').split(',') if v.strip()]
ROOT_URLCONF = 'waviot_telegram.web.urls'
WSGI_APPLICATION = 'waviot_telegram.web.wsgi.application'
INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', 'django.contrib.sessions.middleware.SessionMiddleware', 'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware', 'django.middleware.clickjacking.XFrameOptionsMiddleware', 'django.contrib.auth.middleware.AuthenticationMiddleware', 'waviot_telegram.web.views.SecurityHeaders']
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': values.get('ADMIN_DATABASE', str(BOT_DATABASE.parent / 'admin.sqlite3')), 'OPTIONS': {'timeout': 15}}}
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'DIRS': [BASE_DIR / 'templates'], 'APP_DIRS': True, 'OPTIONS': {'context_processors': ['django.template.context_processors.csrf']}}]
LANGUAGE_CODE = 'ru'
TIME_ZONE = 'Europe/Moscow'
USE_TZ = True
SESSION_COOKIE_AGE = 3600
SESSION_SAVE_EVERY_REQUEST = False
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Strict'
CSRF_COOKIE_SAMESITE = 'Strict'
SESSION_COOKIE_SECURE = PUBLIC
CSRF_COOKIE_SECURE = PUBLIC
SECURE_SSL_REDIRECT = PUBLIC
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https') if PUBLIC else None
SECURE_HSTS_SECONDS = 31536000 if PUBLIC else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LOGGING = {'version': 1, 'disable_existing_loggers': False, 'handlers': {'console': {'class': 'logging.StreamHandler'}}, 'loggers': {'django.security': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False}}}

SECURE_REFERRER_POLICY = 'same-origin'
