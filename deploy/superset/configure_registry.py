"""Run with sudo on 192.168.1.176 to enable the scoped internal HTTP registry."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import tomllib

REGISTRY = '192.168.1.224:80'
NODE_IP = '192.168.1.176'
HOSTS = '''server = "http://192.168.1.224:80"

[host."http://192.168.1.224:80"]
  capabilities = ["pull", "resolve"]
'''


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def registry_path(text):
    data = tomllib.loads(text)
    plugins = data.get('plugins', {})
    for plugin in ('io.containerd.cri.v1.images', 'io.containerd.grpc.v1.cri'):
        path = plugins.get(plugin, {}).get('registry', {}).get('config_path', '')
        if path:
            return path.split(':')[0]
    return ''


def enable_registry_path(text, path):
    data = tomllib.loads(text)
    require(not data.get('imports'), 'Registry config_path is empty and config imports exist. Inspect imported configuration before changing it.')
    require(int(data.get('version', 2)) in (2, 3), 'Expected containerd configuration version 2 or 3')
    plugin = 'io.containerd.cri.v1.images' if data.get('version', 2) == 3 else 'io.containerd.grpc.v1.cri'
    lines = text.splitlines(keepends=True)
    start = end = None
    for index, line in enumerate(lines):
        if not re.match(r'^\s*\[', line):
            continue
        if start is not None:
            end = index
            break
        if line.lstrip().startswith('[['):
            continue
        try:
            probe = tomllib.loads(line+'\n__probe__=true\n')
        except tomllib.TOMLDecodeError:
            continue
        if probe.get('plugins', {}).get(plugin, {}).get('registry', {}).get('__probe__') is True:
            start = index + 1
    replacement = '  config_path = '+json.dumps(path)+'\n'
    if start is None:
        require('registry' not in data.get('plugins', {}).get(plugin, {}),
                'Registry table uses an unsupported inline/dotted form; configuration was not changed')
        updated = text.rstrip()+'\n\n[plugins.'+json.dumps(plugin)+'.registry]\n'+replacement
    else:
        end = len(lines) if end is None else end
        matches = [i for i in range(start, end) if re.match(r'^\s*config_path\s*=', lines[i])]
        require(len(matches) <= 1, 'Multiple registry config_path entries')
        if matches:
            require(re.fullmatch(r'''\s*config_path\s*=\s*(?:"[^"\n]*"|'[^'\n]*')\s*(?:\#.*)?\n?''', lines[matches[0]]),
                    'Unsupported multiline registry path; configuration was not changed')
            lines[matches[0]] = replacement
        else:
            lines.insert(end, replacement)
        updated = ''.join(lines)
    require(registry_path(updated) == path, 'Registry configuration validation failed')
    return updated


def active_config():
    pid = subprocess.check_output(['systemctl', 'show', 'containerd', '--property=MainPID', '--value'], text=True).strip()
    require(pid.isdecimal() and int(pid) > 0, 'containerd is not running')
    argv = Path('/proc/'+pid+'/cmdline').read_bytes().decode().split('\0')
    for index, arg in enumerate(argv):
        if arg in ('--config', '-c'):
            return Path(argv[index+1])
        if arg.startswith('--config='):
            return Path(arg.split('=', 1)[1])
    return Path('/etc/containerd/config.toml')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default=REGISTRY+'/superset:4.1.2-pg-20261116065624')
    args = parser.parse_args()
    require(os.geteuid() == 0, 'Run this script with sudo on the Superset node')
    require(args.image.startswith(REGISTRY+'/'), 'Only the specified internal registry is supported')
    for binary in ('ip', 'containerd', 'systemctl', 'crictl'):
        require(shutil.which(binary), 'Required command missing: '+binary)
    addresses = json.loads(subprocess.check_output(['ip', '-j', '-4', 'address', 'show'], text=True))
    require(any(address.get('local') == NODE_IP for interface in addresses for address in interface.get('addr_info', [])),
            'Wrong machine: run this on 192.168.1.176. No configuration was changed.')
    endpoint = 'unix:///run/containerd/containerd.sock'
    cri = ['crictl', '--runtime-endpoint', endpoint, '--image-endpoint', endpoint]
    subprocess.run(cri+['info'], check=True, stdout=subprocess.DEVNULL)
    config = active_config()
    require(config.is_file(), 'Active containerd configuration file is missing: '+str(config))
    original = config.read_text()
    effective = subprocess.check_output(['containerd', '--config', str(config), 'config', 'dump'], text=True)
    certs = registry_path(effective)
    updated = original
    if not certs:
        certs = '/etc/containerd/certs.d'
        updated = enable_registry_path(original, certs)
    require(Path(certs).is_absolute(), 'Expected an absolute registry hosts directory')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')
    candidate = None
    try:
        if updated != original:
            handle, name = tempfile.mkstemp(prefix='.registry-config-', dir=config.parent)
            candidate = Path(name)
            with os.fdopen(handle, 'w') as output:
                output.write(updated)
            # Validate with the installed containerd before replacing the active file.
            subprocess.run(['containerd', '--config', str(candidate), 'config', 'dump'],
                           check=True, stdout=subprocess.DEVNULL)
        hosts = Path(certs)/REGISTRY/'hosts.toml'
        hosts.parent.mkdir(parents=True, exist_ok=True)
        if hosts.exists():
            shutil.copy2(hosts, str(hosts)+'.backup-'+stamp)
        hosts.write_text(HOSTS)
        os.chmod(hosts, 0o644)
        if candidate:
            backup = Path(str(config)+'.backup-'+stamp)
            shutil.copy2(config, backup)
            os.chmod(candidate, config.stat().st_mode & 0o777)
            os.chown(candidate, config.stat().st_uid, config.stat().st_gid)
            os.replace(candidate, config)
            candidate = None
            try:
                subprocess.run(['systemctl', 'restart', 'containerd'], check=True)
                subprocess.run(['systemctl', 'is-active', '--quiet', 'containerd'], check=True)
                for _ in range(30):
                    probe = subprocess.run(cri+['info'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if probe.returncode == 0:
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError('Kubernetes CRI plugin did not become ready')
            except (subprocess.CalledProcessError, RuntimeError):
                shutil.copy2(backup, config)
                subprocess.run(['systemctl', 'restart', 'containerd'], check=True)
                raise RuntimeError('Containerd restart failed; original configuration restored. Inspect service logs.')
            print('Containerd registry path enabled. Original configuration:', backup)
        print('HTTP registry host configuration:', hosts)
        subprocess.run(cri+['pull', args.image], check=True)
        subprocess.run(cri+['inspecti', args.image], check=True, stdout=subprocess.DEVNULL)
        print('\nImage pull through the Kubernetes CRI service succeeded on 192.168.1.176.')
    finally:
        if candidate and candidate.exists():
            candidate.unlink()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(str(exc))
