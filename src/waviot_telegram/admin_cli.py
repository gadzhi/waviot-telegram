"""Offline bootstrap. Secrets are written only to a new owner-readable file."""
import argparse
import html
import os
import secrets
from pathlib import Path

from .config import load_environment


def prepare():
    os.umask(0o077)
    values = load_environment()
    database = Path(values.get('BOT_DATABASE', 'artifacts/runtime/state.sqlite3'))
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    secret = Path(values.get('ADMIN_SECRET_KEY_FILE', str(database.parent / 'admin-secret.key')))
    if not secret.exists() and not values.get('ADMIN_SECRET_KEY'):
        try:
            with secret.open('x') as f:
                f.write(secrets.token_urlsafe(48))
        except FileExistsError:
            pass
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'waviot_telegram.web.settings')
    import django
    django.setup()
    return database


def main():
    parser = argparse.ArgumentParser(description='Настройка веб-админки Waviot')
    parser.add_argument('command', choices=('bootstrap', 'migrate', 'serve', 'health', 'import-state'))
    parser.add_argument('--username', default='admin')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--bind', default='127.0.0.1:8000')
    parser.add_argument('--source', type=Path)
    args = parser.parse_args()
    database = prepare()
    if args.command == 'import-state':
        import sqlite3
        if not args.source or not args.source.is_file() or database.exists():
            raise SystemExit('Нужна существующая резервная копия --source и пустая целевая база.')
        with args.source.resolve().open('rb'):
            pass
        with database.open('x'):
            pass
        with sqlite3.connect(args.source.resolve().as_uri() + '?mode=ro', uri=True) as source, sqlite3.connect(database) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise SystemExit('Проверка целостности базы не пройдена.')
        print('База импортирована. Polling не запускался.')
        return
    from django.core.management import call_command
    if args.command in ('bootstrap', 'migrate'):
        call_command('migrate', interactive=False, verbosity=0)
        from .storage import Store
        with Store(database):
            pass
    if args.command == 'bootstrap':
        from django.contrib.auth import get_user_model
        from django.db import transaction
        from .access import Access
        from .storage import Store
        output = args.output or database.parent / 'admin-onboarding.html'
        if output.exists() or get_user_model().objects.filter(username=args.username).exists():
            raise SystemExit('Пользователь или файл уже существует. Укажите новое имя и новый файл.')
        password = secrets.token_urlsafe(24)
        with transaction.atomic():
            user = get_user_model().objects.create_superuser(username=args.username, password=password)
            content = '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Первый вход</title><body style="font:18px system-ui;max-width:700px;margin:40px auto"><h1>Первый вход в админку</h1><p>Сохраните пароль в менеджер паролей. После настройки удалите этот файл.</p><p>Логин: <code>' + html.escape(args.username) + '</code></p><p>Пароль: <code>' + html.escape(password) + '</code></p><p><a href="http://127.0.0.1:8000/">Открыть локальную админку</a></p></body></html>'
            with output.open('x') as f:
                f.write(content)
            os.chmod(output, 0o600)
            with Store(database) as store:
                with store.atomic():
                    Access(store).audit(args.username, 'admin_created', user.pk)
        print(f'Настройка создана: {output.resolve()} (права 600). Секреты в консоль не выведены.')
    elif args.command == 'serve':
        from django.conf import settings
        if not settings.PUBLIC and not Path('/.dockerenv').exists() and args.bind.split(':')[0] not in ('127.0.0.1', 'localhost'):
            raise SystemExit('Без ADMIN_PUBLIC=true разрешён только локальный адрес.')
        os.execv(str(Path(os.sys.executable)), [os.sys.executable, '-m', 'gunicorn', 'waviot_telegram.web.wsgi:application', '--bind', args.bind, '--workers', '1', '--threads', '4', '--access-logfile', '-', '--error-logfile', '-'])
    elif args.command == 'health':
        import urllib.request
        request = urllib.request.Request('http://127.0.0.1:8000/health/', headers={'X-Forwarded-Proto': 'https'})
        with urllib.request.urlopen(request, timeout=3) as response:
            if response.status != 200:
                raise SystemExit(1)
