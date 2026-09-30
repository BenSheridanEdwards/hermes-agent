"""Subprocess fixture: actual policy/pool/locks, synthetic provider transport."""
import base64
import io
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

root, profile, provider, mode, gate = sys.argv[1:]
root = Path(root)
home = root / 'profiles' / profile
os.environ['HOME'] = str(root.parent)
os.environ['HERMES_HOME'] = str(home)
import hermes_constants
hermes_constants.get_default_hermes_root = lambda: root
from hermes_cli import auth
from agent.credential_pool import load_pool


def token(exp):
    b = base64.urlsafe_b64encode(json.dumps({'exp': exp}).encode()).decode().rstrip('=')
    return f'fixture.{b}.fixture'


def refresh(*args, **kwargs):
    with (root / 'calls').open('a') as stream:
        stream.write('POST\n')
    if mode == 'outage':
        raise urllib.error.HTTPError('https://synthetic.invalid', 503, 'unavailable', {}, None)
    if mode == 'crash-after-post':
        os._exit(23)  # Lost outcome after remote consumption, before local commit.
    if mode == 'timeout':
        raise TimeoutError('synthetic lost response')
    time.sleep(.08)
    if provider == 'anthropic':
        return io.BytesIO(json.dumps({'access_token': 'synthetic-fresh', 'refresh_token': 'synthetic-next', 'expires_in': 3600}).encode())
    return {'access_token': token(time.time() + 7200), 'refresh_token': 'synthetic-next', 'last_refresh': '2099-01-01T00:00:00Z'}

urllib.request.urlopen = refresh
auth.refresh_codex_oauth_pure = refresh
auth.refresh_xai_oauth_pure = refresh
pool = load_pool(provider)
if mode == 'fail-save':
    def fail_save(*args, **kwargs):
        raise OSError('synthetic disk failure')
    auth._save_auth_store = fail_save
    import agent.credential_pool as cp
    cp._save_auth_store = fail_save
if mode == 'preflight-failure':
    original = os.replace
    def replace(src, dst):
        if 'rotation-intent' in str(dst):
            raise OSError('synthetic preflight disk failure')
        return original(src, dst)
    os.replace = replace
(root / ('ready-' + profile)).touch()
if gate != '-':
    deadline = time.monotonic() + 15
    while not Path(gate).exists():
        if time.monotonic() > deadline:
            raise RuntimeError('fixture gate timeout')
        time.sleep(.01)
try:
    entry = pool.select()
    print(json.dumps({'selected': entry is not None, 'fresh': bool(entry and entry.refresh_token == 'synthetic-next')}))
except Exception as exc:
    print(json.dumps({'selected': False, 'error_type': type(exc).__name__}))
