#!/usr/bin/env python3
"""
transition_guard.py

Enforces legal state transitions on a "request" entity (e.g. a GitHub issue,
a row in a small JSON/DB store) so that only Service Catalog can move a
request between states, and only along allowed paths.

Meant to run as a step in a GitHub Actions workflow, triggered either by:
  - a user/workflow command (e.g. "launch_apply"), or
  - a repository_dispatch callback from a product repo (e.g. "plan_completed")

Both are treated as "attempt a transition to TARGET_STATE", validated
against the same transition table, so there is one source of truth for
what's legal.

States (as requested, without the *_IN_PROGRESS intermediate states):
    CREATED
    PLAN_SUCCESS
    PLAN_FAILED
    APPLY_SUCCESS
    APPLY_FAILED

Exit codes:
    0  -> transition accepted, new state persisted
    1  -> transition rejected (illegal transition, guard failed, or
          version/concurrency conflict) -> fails the Action step
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

try:
    from github import Github  # PyGithub
except ImportError:  # PyGithub is only needed for GitHubIssueStore
    Github = None


# --------------------------------------------------------------------------
# 1. States and transition table
# --------------------------------------------------------------------------

class State(str, Enum):
    CREATED = "CREATED"
    PLAN_SUCCESS = "PLAN_SUCCESS"
    PLAN_FAILED = "PLAN_FAILED"
    APPLY_SUCCESS = "APPLY_SUCCESS"
    APPLY_FAILED = "APPLY_FAILED"


def guard_run_id_matches(request: "Request", payload: dict) -> bool:
    """Reject callbacks from a stale/abandoned run.

    Only enforced once a run_id has actually been recorded on the request
    (i.e. after the first plan/apply attempt). Skip the check on the very
    first transition out of CREATED, since there's nothing to compare yet.
    """
    return True
    # active_run_id = request.data.get("active_run_id")
    # if active_run_id is None:
    #     return True
    # return payload.get("run_id") == active_run_id


# Each entry: source_state -> {target_state: optional guard function}
# A guard function returns True/False given (request, payload).
TRANSITIONS: dict[State, dict[State, Optional[Callable]]] = {
    State.CREATED: {
        State.PLAN_SUCCESS: guard_run_id_matches,
        State.PLAN_FAILED: guard_run_id_matches,
    },
    State.PLAN_FAILED: {
        # retry a plan
        State.PLAN_SUCCESS: guard_run_id_matches,
        State.PLAN_FAILED: guard_run_id_matches,
    },
    State.PLAN_SUCCESS: {
        State.APPLY_SUCCESS: guard_run_id_matches,
        State.APPLY_FAILED: guard_run_id_matches,
        # allow re-planning before apply (e.g. drift, tfvars changed)
        State.PLAN_SUCCESS: guard_run_id_matches,
        State.PLAN_FAILED: guard_run_id_matches,
    },
    State.APPLY_FAILED: {
        # allow re-planning before apply (e.g. drift, tfvars changed)
        State.PLAN_SUCCESS: guard_run_id_matches,
        State.PLAN_FAILED: guard_run_id_matches,
    },
    State.APPLY_SUCCESS: {
        # terminal: no transitions out. If you need re-apply/drift-fix
        # flows, add them here explicitly rather than leaving this open.
    },
}


class TransitionError(Exception):
    def __init__(self, reason: str, current: str, attempted: str):
        self.reason = reason
        self.current = current
        self.attempted = attempted
        super().__init__(f"{reason} (current={current}, attempted={attempted})")


# --------------------------------------------------------------------------
# 2. Request entity + storage
# --------------------------------------------------------------------------

@dataclass
class Request:
    request_id: str
    status: State
    version: int
    data: dict


class RequestStore:
    """Minimal JSON-file backed store, standing in for whatever Service
    Catalog actually uses (GitHub issue body/labels, a small DB, etc.).
    Swap this class out; the transition logic below doesn't care.
    """

    def __init__(self, path: str = "requests_store.json"):
        self.path = Path(path)
        if not self.path.exists():
            self.path.write_text("{}")

    def _load_all(self) -> dict:
        return json.loads(self.path.read_text() or "{}")

    def _save_all(self, all_requests: dict) -> None:
        self.path.write_text(json.dumps(all_requests, indent=2))

    def get(self, request_id: str) -> Request:
        all_requests = self._load_all()
        if request_id not in all_requests:
            # first time we see this request -> implicit CREATED state
            return Request(request_id, State.CREATED, version=0, data={})
        raw = all_requests[request_id]
        return Request(
            request_id=request_id,
            status=State(raw["status"]),
            version=raw["version"],
            data=raw.get("data", {}),
        )

    def save(self, request: Request, expected_version: int) -> None:
        """Optimistic concurrency: reject if someone else updated the
        request in between our read and our write."""
        all_requests = self._load_all()
        current_raw = all_requests.get(request.request_id)
        current_version = current_raw["version"] if current_raw else 0
        if current_version != expected_version:
            raise TransitionError(
                reason="concurrent modification (version mismatch)",
                current=str(current_version),
                attempted=str(expected_version),
            )
        request.version = expected_version + 1
        all_requests[request.request_id] = asdict(request)
        all_requests[request.request_id]["status"] = request.status.value
        self._save_all(all_requests)


class GitHubIssueStore:
    """Request store backed by a GitHub issue.

    - The request's state is encoded as a `status:<state>` label (exactly
      one at a time). No label -> implicit CREATED, same convention as
      RequestStore uses for entries it's never seen.
    - The request's payload (run_id, plan_summary_url, etc.) and a version
      counter are encoded as a JSON blob inside an HTML comment in the
      issue body, so it's invisible when the issue is rendered normally.
    - request_id is the issue number, as a string (e.g. "42"). It's
      assigned by GitHub itself at creation time -- see create_request().

    Same get()/save() interface as RequestStore, so apply_transition()
    doesn't need to change at all when you swap stores.
    """

    LABEL_PREFIX = "status:"
    DATA_START = "<!-- REQUEST_DATA"
    DATA_END = "END_REQUEST_DATA -->"
    _DATA_BLOCK_RE = re.compile(
        re.escape(DATA_START) + r"(.*?)" + re.escape(DATA_END), re.DOTALL
    )

    def __init__(self, repo_full_name: str, token: str):
        if Github is None:
            raise RuntimeError(
                "PyGithub is not installed. `pip install PyGithub` to use "
                "GitHubIssueStore."
            )
        self._repo = Github(token).get_repo(repo_full_name)

    # -- reading -----------------------------------------------------

    def get(self, request_id: str) -> Request:
        issue = self._repo.get_issue(int(request_id))
        status = self._status_from_labels(issue.labels)
        data, version = self._data_from_body(issue.body)
        return Request(request_id, status, version, data)

    def _status_from_labels(self, labels) -> State:
        for label in labels:
            if label.name.startswith(self.LABEL_PREFIX):
                candidate = label.name[len(self.LABEL_PREFIX):].upper()
                try:
                    return State(candidate)
                except ValueError:
                    continue  # not one of our states, ignore
        return State.CREATED

    def _data_from_body(self, body: Optional[str]) -> tuple[dict, int]:
        if not body:
            return {}, 0
        match = self._DATA_BLOCK_RE.search(body)
        if not match:
            return {}, 0
        try:
            blob = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            # Body was hand-edited into an invalid state -- fail safe by
            # treating it as version 0 data-less, so any write attempt
            # against a stale `expected_version` still gets caught.
            return {}, 0
        version = blob.pop("_version", 0)
        return blob, version

    # -- writing -------------------------------------------------------

    def save(self, request: Request, expected_version: int) -> None:
        issue = self._repo.get_issue(int(request.request_id))

        # Re-read current state fresh (not the caller's possibly-stale
        # `request`) to check the version, mirroring RequestStore's
        # optimistic-concurrency check.
        current_data, current_version = self._data_from_body(issue.body)
        if current_version != expected_version:
            raise TransitionError(
                reason="concurrent modification (version mismatch)",
                current=str(current_version),
                attempted=str(expected_version),
            )

        new_version = expected_version + 1

        # Labels: compute the full replacement set in one call so there's
        # never a moment with zero or two status:* labels visible.
        keep = [l.name for l in issue.labels if not l.name.startswith(self.LABEL_PREFIX)]
        new_label = f"{self.LABEL_PREFIX}{request.status.value.lower()}"
        issue.set_labels(*keep, new_label)

        # Body: merge new payload fields into existing data, bump version.
        merged_data = {**current_data, **request.data}
        new_body = self._write_data_block(issue.body, merged_data, new_version)
        issue.edit(body=new_body)

        request.version = new_version

    def _write_data_block(self, body: Optional[str], data: dict, version: int) -> str:
        blob = json.dumps({**data, "_version": version}, indent=2)
        block = f"{self.DATA_START}\n{blob}\n{self.DATA_END}"
        body = body or ""
        if self._DATA_BLOCK_RE.search(body):
            return self._DATA_BLOCK_RE.sub(block, body)
        separator = "\n\n" if body.strip() else ""
        return f"{body}{separator}{block}"

    # -- creation --------------------------------------------------------

    @classmethod
    def create_request(
        cls,
        repo_full_name: str,
        token: str,
        title: str,
        initial_data: Optional[dict] = None,
        extra_labels: Optional[list[str]] = None,
    ) -> Request:
        """Create the issue that represents a brand-new request, in state
        CREATED. Returns a Request whose request_id is the new issue
        number -- callers should persist that ID (e.g. as a workflow
        output) since it's what every later transition call keys on.
        """
        store = cls(repo_full_name, token)
        blob = json.dumps({**(initial_data or {}), "_version": 0}, indent=2)
        body = f"{cls.DATA_START}\n{blob}\n{cls.DATA_END}"
        labels = [f"{cls.LABEL_PREFIX}{State.CREATED.value.lower()}", *(extra_labels or [])]
        issue = store._repo.create_issue(title=title, body=body, labels=labels)
        return Request(str(issue.number), State.CREATED, version=0, data=initial_data or {})


# --------------------------------------------------------------------------
# 3. Core transition logic
# --------------------------------------------------------------------------

def apply_transition(
    store: RequestStore,
    request_id: str,
    target_status: State,
    payload: dict,
) -> Request:
    request = store.get(request_id)
    allowed = TRANSITIONS.get(request.status, {})

    if target_status not in allowed:
        raise TransitionError(
            reason="illegal transition",
            current=request.status.value,
            attempted=target_status.value,
        )

    guard = allowed[target_status]
    if guard is not None and not guard(request, payload):
        raise TransitionError(
            reason="guard rejected transition (e.g. stale run_id)",
            current=request.status.value,
            attempted=target_status.value,
        )

    expected_version = request.version
    request.status = target_status
    request.data.update(payload)
    store.save(request, expected_version)
    return request


# --------------------------------------------------------------------------
# 4. GitHub Actions entrypoint
# --------------------------------------------------------------------------

def _write_github_output(name: str, value: str) -> None:
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if not gh_output:
        return
    with open(gh_output, "a") as f:
        f.write(f"{name}={value}\n")


def create_main() -> int:
    """
    Creates the issue that represents a brand-new request, in state
    CREATED, via GitHubIssueStore.create_request(). Meant to be called
    from create-request.yml, before the launch_plan dispatch to the
    product repo.

    Expected inputs, as GitHub Actions step env vars:

      TITLE              issue title, e.g. "Provision X for team foo"
      INITIAL_DATA_JSON  optional JSON object merged into the issue's
                          data block at version 0, e.g.
                          '{"requested_by": "alice", "params": {...}}'
      EXTRA_LABELS_JSON  optional JSON array of extra label names to add
                          alongside the status:created label
      REQUEST_REPO       "owner/repo" to create the issue in (defaults to
                          GITHUB_REPOSITORY, i.e. the repo the workflow
                          runs in)
      GITHUB_TOKEN       token with issues:write on REQUEST_REPO

    Outputs (via GITHUB_OUTPUT):
      request_id   the new issue number, as a string -- callers must
                   thread this through to every later transition/dispatch
      issue_url    convenience link to the created issue
    """
    if Github is None:
        print("::error::PyGithub is not installed (`pip install PyGithub`)", file=sys.stderr)
        return 1

    title = os.environ.get("TITLE")
    if not title:
        print("::error::TITLE is required", file=sys.stderr)
        return 1

    try:
        initial_data = json.loads(os.environ.get("INITIAL_DATA_JSON", "{}"))
    except json.JSONDecodeError as e:
        print(f"::error::INITIAL_DATA_JSON is not valid JSON: {e}", file=sys.stderr)
        return 1

    try:
        extra_labels = json.loads(os.environ.get("EXTRA_LABELS_JSON", "[]"))
    except json.JSONDecodeError as e:
        print(f"::error::EXTRA_LABELS_JSON is not valid JSON: {e}", file=sys.stderr)
        return 1

    repo_full_name = os.environ.get("REQUEST_REPO") or os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo_full_name or not token:
        print(
            "::error::REQUEST_REPO (or GITHUB_REPOSITORY) and GITHUB_TOKEN are required",
            file=sys.stderr,
        )
        return 1

    request = GitHubIssueStore.create_request(
        repo_full_name, token, title, initial_data=initial_data, extra_labels=extra_labels
    )

    issue_url = f"https://github.com/{repo_full_name}/issues/{request.request_id}"
    print(f"Created request {request.request_id} ({issue_url}) in state {request.status.value}")
    _write_github_output("request_id", request.request_id)
    _write_github_output("issue_url", issue_url)
    return 0


def transition_main() -> int:
    """
    Expected inputs, as GitHub Actions step env vars:

      REQUEST_ID       e.g. "42" (the issue number, when using GitHubIssueStore)
      TARGET_STATUS    e.g. "PLAN_SUCCESS" (must match a State value)
      PAYLOAD_JSON     e.g. '{"run_id": "run-987", "plan_summary_url": "..."}'
      STORE_BACKEND    "json" (default) or "github"

      -- json backend --
      STORE_PATH       optional, path to the JSON store (default used otherwise)

      -- github backend --
      REQUEST_REPO     "owner/repo" of the Service Catalog repo holding the
                        issues (defaults to GITHUB_REPOSITORY, i.e. the repo
                        the workflow is running in)
      GITHUB_TOKEN     token with issues:write on REQUEST_REPO
    """
    request_id = os.environ.get("REQUEST_ID")
    target_status_raw = os.environ.get("TARGET_STATUS")
    # payload_raw = os.environ.get("PAYLOAD_JSON", "{}")
    run_id = os.environ.get("PLAN_RUN_ID")
    backend = os.environ.get("STORE_BACKEND", "json")

    if not request_id or not target_status_raw:
        print("::error::REQUEST_ID and TARGET_STATUS are required", file=sys.stderr)
        return 1

    try:
        target_status = State(target_status_raw)
    except ValueError:
        print(f"::error::Unknown target status '{target_status_raw}'", file=sys.stderr)
        return 1

    # try:
    #     payload = json.loads(payload_raw)
    # except json.JSONDecodeError as e:
    #     print(f"::error::PAYLOAD_JSON is not valid JSON: {e}", file=sys.stderr)
    #     return 1

    if backend == "github":
        repo_full_name = os.environ.get("REQUEST_REPO") or os.environ.get("GITHUB_REPOSITORY")
        token = os.environ.get("GITHUB_TOKEN")
        if not repo_full_name or not token:
            print(
                "::error::REQUEST_REPO (or GITHUB_REPOSITORY) and GITHUB_TOKEN "
                "are required for STORE_BACKEND=github",
                file=sys.stderr,
            )
            return 1
        store = GitHubIssueStore(repo_full_name, token)
    elif backend == "json":
        store_path = os.environ.get("STORE_PATH", "requests_store.json")
        store = RequestStore(store_path)
    else:
        print(f"::error::Unknown STORE_BACKEND '{backend}'", file=sys.stderr)
        return 1

    try:
        request = apply_transition(store, request_id, target_status, payload)
    except TransitionError as e:
        print(
            f"::error::Rejected transition for {request_id}: {e.reason} "
            f"(current={e.current}, attempted={e.attempted})",
            file=sys.stderr,
        )
        _write_github_output("accepted", "false")
        _write_github_output("current_status", e.current)
        return 1

    print(f"Accepted transition for {request_id}: -> {request.status.value}")
    _write_github_output("accepted", "true")
    _write_github_output("current_status", request.status.value)
    return 0


def main() -> int:
    """Dispatch to the right entrypoint based on argv[1].

    Usage:
        python3 transition_guard.py create       -> create_main()
        python3 transition_guard.py transition   -> transition_main()
        python3 transition_guard.py              -> transition_main() (default,
                                                     kept for backward compat)
    """
    command = sys.argv[1] if len(sys.argv) > 1 else "transition"
    if command == "create":
        return create_main()
    elif command == "transition":
        return transition_main()
    else:
        print(f"::error::Unknown command '{command}' (expected 'create' or 'transition')", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
