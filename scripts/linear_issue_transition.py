#!/usr/bin/env python3
"""Move a Linear issue to a given workflow state, optionally reassigning it.

Used by the ready-for-review workflow to keep a Linear issue's state in
sync with the PR working on it - "In Progress" when the PR opens,
"In Review" (plus a specific assignee) when the PR flips to ready.

If the target state doesn't exist yet for the issue's team, it's
created (type "started", positioned right after "In Progress") rather
than failing - this is meant to work without per-team manual setup.

Usage:
    LINEAR_API_KEY=... python3 linear_issue_transition.py \\
        --identifier HOM-47 --to-state "In Progress"
    LINEAR_API_KEY=... python3 linear_issue_transition.py \\
        --identifier HOM-47 --to-state "In Review" --assignee-id <uuid>

Exits non-zero on failure. Intended to be run with continue-on-error (or
equivalent) by its caller - a Linear sync hiccup should never block the
actual PR mechanics.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

GRAPHQL_URL = "https://api.linear.app/graphql"


def gql(token: str, query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=body,
        method="POST",
        headers={"Authorization": token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    if "errors" in result:
        raise SystemExit(f"Linear API error: {result['errors']}")
    return result["data"]


def find_issue_and_states(token: str, identifier: str) -> tuple[str, list[dict]]:
    data = gql(
        token,
        "query($id: String!) { issue(id: $id) { id team { id states { nodes { id name type } } } } }",
        {"id": identifier},
    )
    issue = data.get("issue")
    if not issue:
        raise SystemExit(f"Issue {identifier!r} not found")
    return issue["id"], issue["team"]["id"], issue["team"]["states"]["nodes"]


def find_or_create_state(token: str, team_id: str, states: list[dict], name: str) -> str:
    for state in states:
        if state["name"].lower() == name.lower():
            return state["id"]

    # Not found - create it. Position it right after "In Progress" if that
    # exists, otherwise just let Linear place it.
    in_progress = next((s for s in states if s["name"].lower() == "in progress"), None)
    input_ = {"teamId": team_id, "name": name, "type": "started", "color": "#f2c94c"}
    if in_progress:
        # Linear positions by a float; nudge just after the reference state.
        input_["position"] = 1
    data = gql(
        token,
        "mutation($input: WorkflowStateCreateInput!) { "
        "workflowStateCreate(input: $input) { success workflowState { id } } }",
        {"input": input_},
    )
    return data["workflowStateCreate"]["workflowState"]["id"]


def update_issue(token: str, issue_id: str, state_id: str, assignee_id: str | None) -> None:
    input_ = {"stateId": state_id}
    if assignee_id:
        input_["assigneeId"] = assignee_id
    gql(
        token,
        "mutation($id: String!, $input: IssueUpdateInput!) { "
        "issueUpdate(id: $id, input: $input) { success } }",
        {"id": issue_id, "input": input_},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identifier", required=True, action="append", dest="identifiers")
    parser.add_argument("--to-state", required=True)
    parser.add_argument("--assignee-id", default=None)
    args = parser.parse_args()

    token = os.environ.get("LINEAR_API_KEY")
    if not token:
        print("LINEAR_API_KEY not set - skipping Linear sync.", file=sys.stderr)
        return 1

    ok = True
    for identifier in args.identifiers:
        try:
            issue_id, team_id, states = find_issue_and_states(token, identifier)
            state_id = find_or_create_state(token, team_id, states, args.to_state)
            update_issue(token, issue_id, state_id, args.assignee_id)
            print(f"{identifier}: moved to {args.to_state!r}" + (
                f", assigned to {args.assignee_id}" if args.assignee_id else ""
            ))
        except Exception as exc:  # noqa: BLE001 - report and continue with other identifiers
            print(f"{identifier}: FAILED - {exc}", file=sys.stderr)
            ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
