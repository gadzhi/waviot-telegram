import argparse
import fcntl
import json
import os
import signal
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .bot_config import load_bot_settings
from .collector import collect_inventory, load_seed, refresh_case_snapshots
from .delivery import deliver, recover_delivery, schedule
from .flat_readings import fetch_flat_request
from .navigation import configure_navigation
from .storage import Store
from .telegram_actions import BotActions
from .telegram_client import TelegramClient, TelegramError


def emit(event, **data):
    print(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "event": event, **data}, ensure_ascii=False), flush=True)


def run(settings):
    os.umask(0o077)
    settings.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = settings.database.with_suffix(".lock")
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Бот уже запущен с этой базой данных.") from None
        stop = threading.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
        with Store(settings.database) as store, TelegramClient(settings.token) as api, ThreadPoolExecutor(max_workers=2) as executor:
            me = api.call("getMe")
            webhook = api.call("getWebhookInfo")
            if webhook.get("url"):
                raise ValueError("У этого бота настроен webhook. Локальный polling не запущен; существующая настройка не изменена.")
            if store.get("bot_id") not in (None, me["id"]):
                raise ValueError("Эта база принадлежит другому Telegram-боту. Укажите отдельный BOT_DATABASE.")
            store.set("bot_id", me["id"])
            store.set("pid", os.getpid())
            store.set("started_at", datetime.now(timezone.utc).isoformat())
            load_seed(store)
            refresh_case_snapshots(store)
            recover_delivery(store)
            future = None
            lookup_future = None
            last_scan = 0

            def scan():
                nonlocal future, last_scan
                if future is not None and not future.done():
                    return False
                last_scan = datetime.now(timezone.utc).timestamp()
                future = executor.submit(collect_inventory, settings.database, stop)
                return True

            actions = BotActions(store, api, scan=scan, timezone=settings.timezone)
            info = {"username": me["username"], "pid": os.getpid(), "mode": "local", "automatic_alerts": False}
            info["registration_required"] = True
            # Legacy pairing never grants employee access.
            store.set("pairing_code", None)
            runtime_info = settings.database.parent / "runtime.json"
            runtime_info.write_text(json.dumps(info, ensure_ascii=False, indent=2))
            os.chmod(runtime_info, 0o600)
            emit("started", **info)
            try:
                while not stop.is_set():
                    now = datetime.now(timezone.utc)
                    store.set("heartbeat", now.isoformat())
                    if now.timestamp() - last_scan >= 6 * 3600:
                        scan()
                    if future is not None and future.done():
                        future.result()
                    if lookup_future is not None and lookup_future.done():
                        lookup_future.result()
                        lookup_future = None
                    actions.process(now)
                    if lookup_future is None:
                        request = store.db.execute("SELECT id FROM outbox WHERE kind='flat_lookup' AND state='pending' ORDER BY id LIMIT 1").fetchone()
                        if request:
                            store.job_state(request["id"], "inflight")
                            lookup_future = executor.submit(fetch_flat_request, settings.database, request["id"])
                    schedule(store, now, settings.timezone)
                    try:
                        configure_navigation(store, api)
                        deliver(store, api, now, settings.timezone)
                        updates = api.call("getUpdates", offset=store.offset(), timeout=1 if lookup_future is not None else 10, allowed_updates=["message", "callback_query"])
                        # Persist first, acknowledge with the next getUpdates offset afterwards.
                        store.ingest(updates)
                    except TelegramError as error:
                        store.set("telegram_error", str(error))
                        emit("telegram_error", code=error.code, uncertain=error.uncertain)
                        if error.code in (401, 403, 409):
                            raise
                        stop.wait(min(60, max(1, float(error.retry_after or 5))))
            finally:
                stop.set()
                store.set("stopped_at", datetime.now(timezone.utc).isoformat())
                emit("stopped")


def main():
    parser = argparse.ArgumentParser(description="Локальный Waviot Telegram-бот")
    parser.add_argument("command", nargs="?", choices=("run", "check", "status", "stop", "backup"), default="run")
    parser.add_argument("--output", type=Path, help="Новый файл резервной копии для команды backup")
    arguments = parser.parse_args()
    try:
        settings = load_bot_settings()
        if arguments.command == "run":
            run(settings)
        elif arguments.command == "check":
            with TelegramClient(settings.token) as api:
                me = api.call("getMe")
                webhook = api.call("getWebhookInfo")
                emit("configuration_ok", username=me["username"], webhook_configured=bool(webhook.get("url")), database=str(settings.database), automatic_alerts=False)
        elif arguments.command == "status":
            if not settings.database.exists():
                raise ValueError("Локальная база ещё не создана.")
            with Store(settings.database) as store:
                emit("status", username_id=store.get("bot_id"), paired=store.get("group_id") is not None, heartbeat=store.get("heartbeat"), scan=store.get("scan_status"), open_incidents=len(store.incidents()), uncertain_sends=len(store.uncertain_jobs()))
        elif arguments.command == "stop":
            lock_path = settings.database.with_suffix(".lock")
            with lock_path.open("a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    with Store(settings.database) as store:
                        pid = store.get("pid")
                    if not isinstance(pid, int) or pid <= 1:
                        raise ValueError("Не найден PID запущенного бота.")
                    os.kill(pid, signal.SIGTERM)
                    emit("stop_requested")
                else:
                    emit("not_running")
        elif arguments.command == "backup":
            if not arguments.output or arguments.output.exists() or not settings.database.exists():
                raise ValueError("Укажите новый файл --output; исходная база должна существовать.")
            descriptor = os.open(arguments.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            with sqlite3.connect(settings.database) as source, sqlite3.connect(arguments.output) as destination:
                source.backup(destination)
            emit("backup_created", path=str(arguments.output))
        return 0
    except (ValueError, OSError, TelegramError) as error:
        # Do not serialize requests or arbitrary exception reprs containing token URLs.
        print(str(error) if isinstance(error, (ValueError, TelegramError)) else "Локальная ошибка файла или процесса.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
