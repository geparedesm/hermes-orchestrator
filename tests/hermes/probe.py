"""Drive the orchestration plugin inside the real Hermes container (used by scripts/smoke-phase9.sh).

Loads plugins the way the gateway does, binds a message sender through Hermes's own session context
(as gateway/run_inbound.py does for plugin commands), and calls the slash command and a tool through
Hermes's registry. Prints one JSON object.

Usage (inside the hermes container): python3 probe.py <platform> <user id> <command...>
"""

import json
import sys

from gateway.session_context import clear_session_vars, set_session_vars
from hermes_cli.plugins import discover_plugins, get_plugin_command_handler
from tools.registry import registry

discover_plugins(force=True)
handler = get_plugin_command_handler("orch")
platform, user, command = sys.argv[1], sys.argv[2], " ".join(sys.argv[3:])
result = {"registered": handler is not None,
          "tools": sorted(n for n in ("orch_task_create", "orch_task_status", "orch_task_list", "orch_task_inspect",
                                      "orch_project_list", "orch_approvals_list") if registry.get_entry(n))}
if platform == "tool":
    result["output"] = registry.dispatch(user, json.loads(command))
elif platform == "none":
    result["output"] = handler(command)
else:
    tokens = set_session_vars(platform=platform, user_id=user)
    try:
        result["output"] = handler(command)
    finally:
        clear_session_vars(tokens)
print(json.dumps(result, default=str))
