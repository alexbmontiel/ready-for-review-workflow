# ready-for-review-workflow

A reusable GitHub Action that deterministically flips a draft PR to ready
for review, once:

1. The PR body contains the literal marker `<!-- independent-review: passed -->`
2. Every check run on the PR's head commit has concluded successfully

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
    secrets:
      READY_FOR_REVIEW_PAT: ${{ secrets.READY_FOR_REVIEW_PAT }}
```

Also requires a repo secret named `READY_FOR_REVIEW_PAT`: a fine-grained
PAT with "Pull requests: Read and write" on that repo. `GITHUB_TOKEN` is
documented to be blocked from the `markPullRequestReadyForReview`
mutation, confirmed directly while building this — not theoretical.
Set it with:

```sh
gh secret set READY_FOR_REVIEW_PAT --repo <owner>/<repo>
```
