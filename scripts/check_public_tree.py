"""A small publication guard. Complement it with a secret scanner and review."""
import re
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else '.').resolve()
files = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z']).decode().split('\0')
problems = []
for name in filter(None, files):
    path = root / name
    private = name.startswith(('artifacts/', '.agents/', 'docs/plans/', 'docs/references/')) or name in ('docs/data-audit.md', 'docs/waviot-api-questions.md', 'audit-scope.json', 'skills-lock.json')
    private = private or (Path(name).name.startswith('.env') and name != '.env.example')
    private = private or name.endswith(('.sqlite3', '.sqlite', '.db', '.pem', '.key', '.p12', '.pfx', '.log', '.bak'))
    if private or path.is_symlink():
        problems.append((name, 'private file or symlink'))
        continue
    data = path.read_bytes()
    rules = {
        'local user path': rb'/(?:Users|home)/[A-Za-z0-9_.-]+/',
        'Telegram token pattern': rb'\b[0-9]{5,}:[A-Za-z0-9_-]{25,}\b',
        'private key': rb'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----',
    }
    for label, pattern in rules.items():
        if re.search(pattern, data):
            problems.append((name, label))
    for value in re.findall(rb'(?<![0-9])([+][1-9][0-9]{7,14})(?![0-9])', data):
        phone = value.decode()
        fictional_prefix = '+' + '1202' + '555'
        if not (phone.startswith(fictional_prefix) and len(phone) == 12 and 100 <= int(phone[-4:]) <= 199):
            problems.append((name, 'non-fictional phone pattern'))
if problems:
    for name, reason in problems:
        print(f'{name}: {reason}')
    raise SystemExit(1)
print(f'Publication guard passed: {sum(bool(f) for f in files)} tracked files; no prohibited paths or detected private patterns.')
