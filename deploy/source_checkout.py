"""Validate the pulled source and export an immutable build without changing Git."""
import hashlib
from pathlib import Path
import subprocess
import sys


def source_commit(root):
    root = Path(root).resolve()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
    if Path(git('rev-parse', '--show-toplevel')).resolve() != root:
        raise ValueError('Run the checked-in launcher from the GitHub project root.')
    origin = git('remote', 'get-url', 'origin').removesuffix('.git').rstrip('/')
    if origin not in ('https://github.com/burodw1024/cloud', 'git@github.com:burodw1024/cloud'):
        raise ValueError('This checkout must use burodw1024/cloud on GitHub as origin. No Gitea access is required.')
    if git('status', '--porcelain', '--untracked-files=normal'):
        raise ValueError('The source checkout has local changes. Preserve them, then use a clean GitHub checkout; this launcher never resets or commits them.')
    commit = git('rev-parse', 'HEAD')
    expected = subprocess.check_output(['git', '-C', str(root), 'show', commit + ':deploy-agentic-updated.sh'])
    if expected != (root/'deploy-agentic-updated.sh').read_bytes():
        raise ValueError('The launcher differs from the pulled commit.')
    return commit


if __name__ == '__main__':
    try:
        print(source_commit(sys.argv[1]))
    except (ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc))
