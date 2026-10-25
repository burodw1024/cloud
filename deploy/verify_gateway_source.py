"""Verify known live hotfixes are included; never rewrite pulled code on-prem."""
import ast
import hashlib
import json
from pathlib import Path
import re
import sys


def canonical_node(node):
    """Ignore absent/empty AST metadata added between Python minor versions."""
    if isinstance(node, ast.AST):
        return [type(node).__name__, [
            [name, canonical_node(value)] for name, value in ast.iter_fields(node)
            if value is not None and value != []
        ]]
    if isinstance(node, list):
        return [canonical_node(value) for value in node]
    if isinstance(node, bytes):
        return {"bytes": node.hex()}
    return node


def normalize(source):
    for label in ('OLLAMA_OPTIONS', 'REPORT_PROJECTION'):
        source = re.sub(r'(?m)^ *# BEGIN_AGENTIC_' + label + r'\n.*?^ *# END_AGENTIC_' + label + r'\n', '', source, flags=re.S|re.M)
    tree = ast.parse(source)
    class RemoveKnownOptions(ast.NodeTransformer):
        def visit_Expr(self, node):
            value = node.value
            if (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                    and isinstance(value.func.value, ast.Name) and value.func.value.id == 'payload'
                    and value.func.attr == 'update' and not value.args and value.keywords
                    and {key.arg for key in value.keywords} <= {'think', 'options', 'keep_alive'}):
                for keyword in value.keywords:
                    expected = {'think': False, 'options': {'num_predict': 256}, 'keep_alive': '30m'}[keyword.arg]
                    try:
                        actual = ast.literal_eval(keyword.value)
                    except ValueError:
                        return node
                    if actual != expected:
                        return node
                return None
            return node
    tree = RemoveKnownOptions().visit(tree)
    # Removing an if's only known-options expression leaves an empty body.
    class RemoveEmpty(ast.NodeTransformer):
        def visit_If(self, node):
            self.generic_visit(node)
            return node if node.body or node.orelse else None
    tree = RemoveEmpty().visit(tree)
    return hashlib.sha256(json.dumps(canonical_node(tree), ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def verify(live, baked, baseline):
    previous_file = Path(__file__).with_name('gateway_previous.sha256')
    previous = previous_file.read_text().splitlines() if previous_file.is_file() else []
    recognized_previous = bool(previous) and hashlib.sha256(live.encode()).hexdigest() in previous
    if live != baked and not recognized_previous and normalize(live) != baseline:
        raise ValueError('Live MCP code contains an unrecognized change. Snapshot retained; incorporate that change in the cloud source before deployment. No code was overwritten.')
    tree = ast.parse(baked)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == 'update'
             and isinstance(node.func.value, ast.Name) and node.func.value.id == 'payload']
    expected = {'think': False, 'options': {'num_predict': 256}, 'keep_alive': '30m'}
    found = False
    for call in calls:
        try:
            values = {keyword.arg: ast.literal_eval(keyword.value) for keyword in call.keywords}
            found = found or all(values.get(key) == value for key, value in expected.items())
        except (ValueError, TypeError):
            pass
    if not found or 'BEGIN_AGENTIC_REPORT_PROJECTION' not in baked:
        raise ValueError('The pulled cloud source is missing the reviewed MCP options or report projection.')


if __name__ == '__main__':
    try:
        root = Path(sys.argv[2])
        baseline = (root/'deploy/gateway_baseline.sha256').read_text().strip()
        verify(Path(sys.argv[1]).read_text(), (root/'agentic_ai/mcp_gateway.py').read_text(), baseline)
        print('Live MCP compatibility verified. The image will use the pulled cloud code unchanged.')
    except ValueError as exc:
        raise SystemExit(str(exc))
