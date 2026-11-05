import hashlib
import sqlite3
import socket
from ipaddress import ip_address
from pathlib import Path
from datetime import datetime, timezone
from functools import wraps

from django import forms
from django.conf import settings
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST, require_safe

from waviot_telegram.access import Access
from waviot_telegram.storage import Store


class SecurityHeaders:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response['Cache-Control'] = 'no-store'
        response['Referrer-Policy'] = 'same-origin'
        response['X-Frame-Options'] = 'DENY'
        response['Content-Security-Policy'] = "default-src 'none'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        return response


class LoginForm(forms.Form):
    username = forms.CharField(label='Логин', max_length=150)
    password = forms.CharField(label='Пароль', widget=forms.PasswordInput)


class EmployeeForm(forms.Form):
    name = forms.CharField(label='Имя сотрудника', max_length=100)
    phone = forms.CharField(label='Телефон', max_length=30, help_text='Международный формат: +12025550101')


def protected(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_staff:
            return redirect('login')
        return view(request, *args, **kwargs)
    return wrapped


def client_address(request):
    peer = request.META.get('REMOTE_ADDR', 'unknown')
    proxy = settings.ADMIN_TRUSTED_PROXY
    if not settings.PUBLIC or not proxy:
        return peer
    try:
        try:
            trusted = {ip_address(proxy)}
        except ValueError:
            # Docker service addresses may change when containers are recreated.
            trusted = {ip_address(item[4][0]) for item in socket.getaddrinfo(proxy, None, type=socket.SOCK_STREAM)}
        if ip_address(peer) in trusted:
            # Caddy replaces this header with one address; do not accept a chain.
            return str(ip_address(request.META.get('HTTP_X_FORWARDED_FOR', '')))
    except (ValueError, OSError):
        pass
    return peer


def login(request):
    form = LoginForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        now = datetime.now(timezone.utc)
        # Accept a forwarded address only from the explicitly configured proxy.
        name = form.cleaned_data['username'].casefold()
        keys = ['web:user:' + hashlib.sha256(name.encode()).hexdigest(), 'web:ip:' + client_address(request)]
        with Store(settings.BOT_DATABASE) as store:
            access = Access(store)
            with store.atomic():
                blocked = any(access.limited(key, now, 10) for key in keys)
                for key in keys:
                    access.attempt(key, now)
            user = None if blocked else authenticate(request, username=form.cleaned_data['username'], password=form.cleaned_data['password'])
            if user and user.is_staff and user.is_active:
                auth_login(request, user)
                with store.atomic():
                    access.audit(user.username, 'admin_login', user.pk)
                return redirect('employees')
        form.add_error(None, 'Вход не выполнен. Проверьте данные или повторите через 15 минут.')
    return render(request, 'web/login.html', {'form': form})


@require_POST
@protected
def logout(request):
    auth_logout(request)
    return redirect('login')


@protected
def employees(request):
    form = EmployeeForm(request.POST or None)
    code = None
    with Store(settings.BOT_DATABASE) as store:
        access = Access(store)
        if request.method == 'POST' and form.is_valid():
            try:
                _, code = access.create(**form.cleaned_data, actor=request.user.username)
                form = EmployeeForm()
            except (ValueError, sqlite3.IntegrityError) as error:
                form.add_error(None, str(error) if isinstance(error, ValueError) else 'Этот телефон уже добавлен.')
        return render(request, 'web/employees.html', {'form': form, 'employees': access.employees(), 'code': code})


@require_POST
@protected
def employee_action(request, employee_id):
    code, error = None, None
    with Store(settings.BOT_DATABASE) as store:
        access = Access(store)
        try:
            action = request.POST.get('action')
            if action == 'disable':
                access.disable(employee_id, request.user.username)
            elif action in ('invite', 'rebind'):
                code = access.invite(employee_id, actor=request.user.username, rebind=action == 'rebind')
            else:
                return HttpResponse(status=400)
        except ValueError as exc:
            error = str(exc)
        return render(request, 'web/employees.html', {'form': EmployeeForm(), 'employees': access.employees(), 'code': code, 'error': error})


@protected
def group(request):
    code, error = None, None
    with Store(settings.BOT_DATABASE) as store:
        access = Access(store)
        if request.method == 'POST':
            try:
                if request.POST.get('action') == 'disconnect':
                    access.disconnect_group(request.user.username)
                elif request.POST.get('action') == 'invite':
                    code = access.group_invite(request.user.username)
                else:
                    return HttpResponse(status=400)
            except ValueError as exc:
                error = str(exc)
        return render(request, 'web/group.html', {'group_id': store.get('group_id'), 'code': code, 'error': error})


@protected
def audit(request):
    from waviot_telegram.messages import local_time
    labels = {'employee_created': 'Сотрудник добавлен', 'employee_rebind': 'Аккаунт отвязан, выдан новый код',
              'invitation_issued': 'Выдан код регистрации', 'employee_disabled': 'Доступ сотрудника отключён',
              'employee_registered': 'Сотрудник зарегистрирован', 'group_invitation_issued': 'Выдан код подключения группы',
              'group_connected': 'Группа подключена', 'group_disconnected': 'Группа отключена',
              'admin_recovery_used': 'Использован резервный код', 'admin_login': 'Вход администратора', 'admin_created': 'Администратор создан'}
    with Store(settings.BOT_DATABASE) as store:
        events = [dict(r) for r in store.db.execute('SELECT actor,action,subject,at FROM access_audit ORDER BY id DESC LIMIT 100')]
    for event in events:
        event['at'] = local_time(event['at'])
        event['action'] = labels.get(event['action'], event['action'])
    return render(request, 'web/audit.html', {'events': events})


def health(request):
    return HttpResponse('ok', content_type='text/plain')


@require_safe
def admin_css(request):
    return HttpResponse(Path(__file__).with_name('admin.css').read_text(), content_type='text/css; charset=utf-8')
