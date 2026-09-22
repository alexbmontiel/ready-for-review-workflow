# ready-for-review-workflow

A reusable GitHub Action that deterministically flips a draft PR to ready
for review, once:

1. The PR body contains the literal marker `<!-- independent-review: passed -->`
2. Every check run on the PR's head commit has concluded successfully

It also keeps the Linear issue(s) a PR references in sync with the PR's
own lifecycle, deterministically rather than relying on an agent to
remember to do it by hand:

- **PR opens → issue moves to "In Progress"**
- **PR flips to ready (the moment above) → issue moves to "In Review"**,
  and is reassigned to a configured reviewer, if one is set

Both directions read the issue identifier(s) straight out of the PR body
(`Closes HOM-47`, `Fixes THE-12`, etc. - same convention Linear's own
magic words use), and both are best-effort: a Linear API hiccup logs a
warning and never blocks the actual PR mechanics. If the referenced
issue's team doesn't have an "In Review" state yet, one is created
automatically the first time it's needed - no manual per-team setup.

This is a separate concern from Linear's native GitHub integration (which
handles the diff-tab/PR-linking UI, and just needs to be connected once
per workspace, plus a `Closes <ID>` reference in the PR body - no
separate wiring needed here).

Public (not private) specifically so it can be called from repos on *any*
GitHub account — private reusable workflows can only be shared with other
repos owned by the same user or organization, which doesn't work across
the two-account split this tooling runs under (see
[alex-mf-montiel/agentic-workflow](https://github.com/alex-mf-montiel/agentic-workflow)'s
`DESIGN.md` for the full history). Nothing sensitive lives in this repo —
the PAT it needs is supplied by each caller's own repo secret.

## Usage

Copy this into the calling repo at `.github/workflows/ready-for-review.yml`:

```yaml
name: Ready for review

on:
  pull_request:
    types: [opened, synchronize, reopened, edited]
  check_suite:
    types: [completed]

permissions:
  pull-requests: write
  checks: read
  contents: read

jobs:
  ready-for-review:
    uses: alexbmontiel/ready-for-review-workflow/.github/workflows/ready-for-review.yml@main
    with:
      linear_reviewer_id: "<linear-user-uuid, optional>"
    secrets:
      READY_FOR_REVIEW_PAT: ${{ secrets.READY_FOR_REVIEW_PAT }}
      LINEAR_API_KEY: ${{ secrets.LINEAR_API_KEY }}
```

Requires a repo secret named `READY_FOR_REVIEW_PAT`: a fine-grained
PAT with "Pull requests: Read and write" on that repo. `GITHUB_TOKEN` is
documented to be blocked from the `markPullRequestReadyForReview`
mutation, confirmed directly while building this — not theoretical.
Set it with:

```sh
gh secret set READY_FOR_REVIEW_PAT --repo <owner>/<repo>
```

Optionally, for the Linear status-sync behavior: a repo secret named
`LINEAR_API_KEY` (a Linear **Personal API key** - Linear Settings → API →
Personal API keys - scoped to whichever workspace this repo's issues live
in), and a `linear_reviewer_id` input (that workspace's Linear user ID
for whoever should be assigned when a PR is ready - find it by querying
`{ users { nodes { id name } } }` against that workspace's API). Leave
both unset to skip Linear syncing entirely and keep only the GitHub-side
ready-for-review mechanics.

```sh
gh secret set LINEAR_API_KEY --repo <owner>/<repo>
```
