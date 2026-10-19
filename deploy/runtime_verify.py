import hashlib
import os
from pathlib import Path
import sys

# Absolute-file execution starts in /app/deploy, not the Django project root.
APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))
os.chdir(APP_ROOT)
os.environ.setdefault('DJANGO_SETTINGS_MODULE','buro_first.settings_production')
import django
django.setup()
from django.conf import settings
from agentic_ai.mcp_gateway import load_config
config=load_config()
assert settings.POLICY_WORK_DIR=='/data/policy_rag'
assert config['url']=='http://192.168.1.99:30444/servers/3af4c69246314595a30043f0b8d8b654/mcp'
assert config['timeout']==300 and config['request_timeout']==600
assert config['token']
args=Path('/proc/1/cmdline').read_bytes().split(b'\0')
for flag,value in ((b'--workers',b'1'),(b'--worker-class',b'gthread'),(b'--threads',b'4'),(b'--timeout',b'660')):
    assert args[args.index(flag)+1]==value
assert os.environ['POLICY_WORK_DIR']=='/data/policy_rag'
print('Effective Django, policy directory, Gunicorn and MCP settings verified. Credentials retained.')
print('Gateway code sha256:',hashlib.sha256(Path('/app/agentic_ai/mcp_gateway.py').read_bytes()).hexdigest())
