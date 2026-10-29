"""Patch only the known ContextForge v1.0.10 read-timeout field bound.

Fail closed on an unrecognized field definition; leave all other settings intact.
"""
import ast
from pathlib import Path
import sys


def patch(path):
    path = Path(path)
    source = path.read_text()
    tree = ast.parse(source)
    matches = [n for n in ast.walk(tree) if isinstance(n, ast.AnnAssign)
               and isinstance(n.target, ast.Name) and n.target.id == 'httpx_read_timeout']
    if len(matches) != 1 or not isinstance(matches[0].value, ast.Call):
        raise ValueError('Unrecognized ContextForge read-timeout declaration')
    call = matches[0].value
    bounds = [k.value for k in call.keywords if k.arg == 'le']
    if len(bounds) != 1 or ast.literal_eval(bounds[0]) != 600.0:
        raise ValueError('Expected the reviewed v1.0.10 600-second upper bound')
    bound = bounds[0]
    lines = source.encode().splitlines(keepends=True)
    start = sum(map(len, lines[:bound.lineno - 1])) + bound.col_offset
    end = sum(map(len, lines[:bound.end_lineno - 1])) + bound.end_col_offset
    content = source.encode()
    updated = content[:start] + b'1800.0' + content[end:]
    ast.parse(updated.decode())
    path.write_bytes(updated)


if __name__ == '__main__':
    patch(sys.argv[1])
