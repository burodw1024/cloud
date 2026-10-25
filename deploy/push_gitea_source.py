"""Mirror the exact reviewed commit to a new release ref without force or rewrites."""
from pathlib import Path
import os
import subprocess
import sys

URL = 'http://192.168.1.222/admin/agentic.git'


def push_source(root, revision):
    branch = 'refs/heads/agentic/releases/' + revision
    if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
        raise ValueError('Expected a full reviewed Git commit')
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '1', 'GIT_TRACE': '0', 'GIT_TRACE_CURL': '0'}
    command = ['git', '-C', str(Path(root).resolve())]
    # Credentials are requested by Git on the terminal; none are put in a URL or file.
    subprocess.run(command + ['push', URL, revision + ':' + branch], env=env, check=True)
    actual = subprocess.check_output(command + ['ls-remote', '--heads', URL, branch], env=env, text=True)
    if actual.strip().split() != [revision, branch]:
        raise ValueError('Gitea release reference did not match the reviewed commit')
    print('Gitea verified reviewed release ref:', branch)


if __name__ == '__main__':
    push_source(sys.argv[1], sys.argv[2])
