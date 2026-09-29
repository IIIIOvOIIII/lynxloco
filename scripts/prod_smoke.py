"""Read-only HTTP smoke; does not log into a host or use credentials."""
import json
import os
import urllib.error
import urllib.request

base = os.environ.get('MILOCO_SMOKE_BASE_URL', 'http://miloco.esxi:1810').rstrip('/')
results = {}
for path, expected in (('/health', 200), ('/', 200), ('/api/cameras', 401)):
    try:
        with urllib.request.urlopen(base + path, timeout=10) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    results[path] = status
    if status != expected:
        raise SystemExit(f'{path}: expected {expected}, received {status}')
print(json.dumps(results, sort_keys=True))
