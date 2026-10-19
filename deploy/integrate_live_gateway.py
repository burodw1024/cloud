"""Preserve live gateway code and apply narrowly checked compatibility hooks."""
import ast
import re
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text()
root = Path(sys.argv[2])
# Replace only blocks emitted by this integrator on an earlier successful release.
for label in ("OLLAMA_OPTIONS", "REPORT_PROJECTION"):
    begin = "    # BEGIN_AGENTIC_" + label
    end = "    # END_AGENTIC_" + label
    if begin in source or end in source:
        if source.count(begin) != 1 or source.count(end) != 1:
            raise SystemExit("Ambiguous prior integration markers")
        source = re.sub(r"(?m)^ *# BEGIN_AGENTIC_" + label + r"\n.*?^ *# END_AGENTIC_" + label + r"\n", "", source, flags=re.S|re.M)
tree = ast.parse(source)
# Refuse to publish live source containing literal credentials.
sensitive = {"token", "password", "api_key", "secret", "authorization", "model_key"}
for item in ast.walk(tree):
    if isinstance(item, ast.Assign) and isinstance(item.value, ast.Constant) and isinstance(item.value.value, str) and item.value.value:
        for target in item.targets:
            if isinstance(target, ast.Name) and any(word in target.id.lower() for word in sensitive):
                raise SystemExit("Live gateway contains a credential-like literal; move it to existing secret configuration before publishing.")
    if isinstance(item, ast.Dict):
        for key, value in zip(item.keys, item.values):
            if (isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value.lower() in sensitive
                    and isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value):
                raise SystemExit("Live gateway contains a literal credential in a mapping; refusing publication.")
functions = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
for name in ("_select_tool", "_run", "_model_response", "gateway_enabled", "_validate_call"):
    if name not in functions:
        raise SystemExit("Live MCP gateway differs from the supported interface: " + name)
# Do not remove or regenerate the live fixes; modify only the native payload.
node = functions["_select_tool"]
assignments = [item for item in node.body if isinstance(item, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "payload" for t in item.targets)]
if len(assignments) != 1:
    raise SystemExit("Cannot safely locate live model-selection payload.")
lines = source.splitlines(keepends=True)
lines.insert(assignments[0].end_lineno,
    '    # BEGIN_AGENTIC_OLLAMA_OPTIONS\n'
    '    if config["provider"] == "ollama":\n'
    '        payload.update(think=False, options={"num_predict": 256}, keep_alive="30m")\n'
    '    # END_AGENTIC_OLLAMA_OPTIONS\n')
source = ''.join(lines)
marker = '            texts = [item.text for item in result.content if item.type == "text"]'
if source.count(marker) != 1:
    raise SystemExit("Cannot safely integrate report projection into this live gateway version.")
hook = '''            # BEGIN_AGENTIC_REPORT_PROJECTION
            projection_tools = {x.strip() for x in get_setting("MCP_REPORT_PROJECTION_TOOLS", "loan-due-report").split(",") if x.strip()}
            if name in projection_tools and not result.isError:
                from .report_presentation import present_report
                try:
                    presentation = await present_report(config, query, raw, _model_response)
                except Exception as exc:
                    raise GatewayError("report_presentation_failed", "The tool completed, but its report could not be safely formatted. No unfiltered data was returned. Check the audit log before retrying.") from exc
                return {"ok": True, "tool": name, "plan": plan, "tool_executed": True, **presentation}
            # END_AGENTIC_REPORT_PROJECTION
'''
source = source.replace(marker, hook + marker)
ast.parse(source)
(root/'agentic_ai/mcp_gateway.py').write_text(source)
print("Live gateway preserved; native Ollama fix and checked report projection integrated.")
