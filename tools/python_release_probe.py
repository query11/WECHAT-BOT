"""Capture official Python release provenance in CI; no unverified build output."""
import base64
import hashlib
import json
from pathlib import Path
import urllib.request

VERSION = '3.12.10'
NAME = 'python-' + VERSION + '-embed-amd64.zip'
BASE = 'https://www.python.org/ftp/python/' + VERSION + '/'
output = Path('.runtime/python-provenance')
output.mkdir(parents=True, exist_ok=True)
with urllib.request.urlopen(BASE + NAME, timeout=120) as response:
    data = response.read()
digest = hashlib.sha256(data).hexdigest()
with urllib.request.urlopen(BASE + NAME + '.sigstore', timeout=60) as response:
    bundle = response.read()
published = json.loads(bundle)
assert base64.b64decode(published['messageSignature']['messageDigest']['digest']).hex() == digest
entry = json.loads(base64.b64decode(published['verificationMaterial']['tlogEntries'][0]['canonicalizedBody']))
assert entry['spec']['data']['hash']['value'] == digest
(output / (NAME + '.sigstore')).write_bytes(bundle)
(output / 'python.json').write_text(json.dumps({'version': VERSION, 'url': BASE + NAME,
    'sha256': digest, 'bytes': len(data)}, indent=2), encoding='utf-8')
print('Official Python embedded SHA256:', digest, flush=True)
