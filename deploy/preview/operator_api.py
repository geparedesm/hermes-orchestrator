"""Use the operator's configured internal API credential without exporting it to the host."""
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
CLIENT = '''import json,sys,urllib.request,urllib.error
from control_plane.settings import Settings
s=Settings.from_env()
token=next(k for k,v in s.tokens.items() if v=='operator')
a=json.load(sys.stdin)
request=urllib.request.Request('http://127.0.0.1:8080'+a['path'],method=a['method'],
 data=json.dumps(a['body']).encode() if a['body'] is not None else None,
 headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
try:
 with urllib.request.urlopen(request,timeout=60) as response: print(response.read().decode())
except urllib.error.HTTPError as exc:
 print(json.dumps({'api_error':exc.code,'detail':exc.read().decode()}))
 sys.exit(1)
'''


def api(method, path, body=None):
    result = subprocess.run(['docker', 'compose', 'exec', '-T', 'control-plane', 'python', '-c', CLIENT],
                            cwd=ROOT, input=json.dumps({'method': method, 'path': path, 'body': body}),
                            text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stdout.strip() or 'Internal operator API request failed')
    return json.loads(result.stdout)
