"""Read-only heartbeat probe; never opens a polling connection."""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def main():
    path = Path(os.environ.get('BOT_DATABASE', '/data/state.sqlite3')).resolve()
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2) as db:
            row = db.execute("SELECT value FROM settings WHERE key='heartbeat'").fetchone()
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(json.loads(row[0]))).total_seconds()
        return 0 if 0 <= age < 120 else 1
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
