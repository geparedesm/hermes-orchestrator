"""Task API client shared by the plugin's tools, slash command, CLI, and Dashboard routes.

Standard library only (it runs inside the official Hermes image). Every call carries the plugin's
service token and the human principal it acts for; mutating calls carry an Idempotency-Key. The
control plane decides what each principal may do; this client holds no task state.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

DEFAULT_URL = "http://control-plane:8080"
TIMEOUT_SECONDS = 15


UNKNOWN = -1  # the request may have been applied: it was sent and no answer arrived


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _token() -> str:
    path = os.environ.get("HO_PLUGIN_TOKEN_FILE", "/run/secrets/ho_plugin_token")
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError as exc:
        raise ApiError(0, f"the orchestrator token is not available ({path})") from exc


class TaskApi:
    def __init__(self, principal: str, base_url: str | None = None) -> None:
        self.principal = principal
        self.base_url = (base_url or os.environ.get("HO_API_URL") or DEFAULT_URL).rstrip("/")

    def call(self, method: str, path: str, body: dict[str, Any] | None = None,
             params: dict[str, str] | None = None) -> Any:
        url = self.base_url + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        headers = {"Authorization": f"Bearer {_token()}", "X-HO-Principal": self.principal,
                   "Accept": "application/json"}
        data = None
        if method != "GET":
            headers["Idempotency-Key"] = f"hermes-{uuid.uuid4()}"
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - fixed internal URL
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(detail)
                message = parsed.get("message") or parsed.get("detail") or detail
            except ValueError:
                message = detail
            raise ApiError(exc.code, str(message)[:500]) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (ConnectionRefusedError, ConnectionResetError)) or "Name or service" in str(exc.reason):
                raise ApiError(0, f"the orchestrator is unreachable: {exc.reason}") from exc
            raise ApiError(UNKNOWN if method != "GET" else 0, f"no answer from the orchestrator: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            # Sent, but no answer: a mutation may have been applied.
            raise ApiError(UNKNOWN if method != "GET" else 0, f"no answer from the orchestrator: {exc}") from exc
        return json.loads(raw) if raw else {}

    # ------------------------------------------------------------ operations

    def projects(self) -> Any:
        return self.call("GET", "/v1/projects")

    def tasks(self, project: str | None = None, state: str | None = None, active: bool = False) -> Any:
        params = {k: v for k, v in (("project", project), ("state", state)) if v}
        if active:
            params["active"] = "true"
        return self.call("GET", "/v1/tasks", params=params or None)

    def task(self, key: str) -> Any:
        return self.call("GET", f"/v1/tasks/{_key(key)}")

    def inspect(self, key: str) -> Any:
        return self.call("GET", f"/v1/tasks/{_key(key)}/orchestration")

    def create(self, project: str, request: str, title: str | None = None, priority: str | None = None) -> Any:
        body: dict[str, Any] = {"project": project, "request": request}
        if title:
            body["title"] = title
        if priority:
            body["priority"] = priority.upper()
        return self.call("POST", "/v1/tasks", body)

    def action(self, key: str, verb: str) -> Any:
        if verb not in ("pause", "resume", "cancel", "retry"):
            raise ApiError(400, f"unknown task action {verb}")
        return self.call("POST", f"/v1/tasks/{_key(key)}/{verb}")

    def revise(self, key: str, text: str) -> Any:
        return self.call("POST", f"/v1/tasks/{_key(key)}/revise", {"text": text})

    def budget(self, key: str, add: dict[str, int] | None = None) -> Any:
        if add:
            return self.call("POST", f"/v1/tasks/{_key(key)}/budget", {"add": add})
        return self.call("GET", f"/v1/tasks/{_key(key)}/budget")

    def summary(self) -> Any:
        return self.call("GET", "/v1/dashboard/summary")

    def task_view(self, key: str) -> Any:
        return self.call("GET", f"/v1/dashboard/tasks/{_key(key)}")

    def workers(self) -> Any:
        return self.call("GET", "/v1/workers")

    def manifest(self, key: str, manifest_id: str) -> Any:
        return self.call("GET", f"/v1/tasks/{_key(key)}/manifests/{urllib.parse.quote(manifest_id, safe='')}")

    def generate_manifest(self, key: str) -> Any:
        return self.call("POST", f"/v1/tasks/{_key(key)}/manifest")

    def approvals(self, include_decided: bool = False) -> Any:
        return self.call("GET", "/v1/approvals", params={"state": "" if include_decided else "PENDING"})

    def decide(self, approval_id: str, approve: bool, note: str | None = None) -> Any:
        body: dict[str, Any] = {"decision": "APPROVE" if approve else "REJECT"}
        if note:
            body["note"] = note
        return self.call("POST", f"/v1/approvals/{urllib.parse.quote(approval_id, safe='')}/decision", body)


def _key(key: str) -> str:
    key = key.strip().upper()
    if not key.startswith("T-") or not key[2:].isdigit():
        raise ApiError(400, f"task keys look like T-12, not {key!r}")
    return key


# ---------------------------------------------------------------- rendering


def task_line(task: dict[str, Any]) -> str:
    return f"{task['key']} [{task['state']}] {task.get('title') or ''}".strip()


def task_summary(task: dict[str, Any]) -> str:
    lines = [task_line(task)]
    for field in ("project", "priority", "state_reason"):
        if task.get(field):
            lines.append(f"  {field.replace('_', ' ')}: {task[field]}")
    return "\n".join(lines)


def approval_line(approval: dict[str, Any]) -> str:
    return f"{approval['id']} {approval['action']} — {approval.get('summary') or ''}".strip()
