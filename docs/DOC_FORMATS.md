# Documentation Formats

Canonical formats for every human-facing document this repo produces: commit
messages, PR titles and bodies, CHANGELOG entries, and GitHub release notes.
AI agents and humans alike: follow these exactly. On the maintainer's machine
a Claude Code hook enforces the commit/PR/release formats at the moment of
`git commit` / `gh pr create` / `gh release`. Release *process* (CI, PyPI
publishing) is documented separately in `docs/RELEASE_GUIDE.md`.

## Commit messages

Conventional Commits:

```
type(scope)!: subject
```

- **Types:** `feat` `fix` `docs` `refactor` `perf` `test` `build` `ci`
  `chore` `revert`.
- **Scope:** optional, lowercase kebab-case (e.g. `auth`, `sync`, `blog`).
- **Subject:** imperative mood ("add", not "added"); the whole first line is
  ≤ 72 characters; no trailing period.
- **Body (optional):** explains *why*, wrapped at 72 columns.
- **Breaking changes:** `!` after the type/scope plus a `BREAKING CHANGE:`
  footer.
- Exempt from the rule: merge commits, `Revert …`, `fixup!`, `squash!`, and
  `chore(release): …`.

Examples:

```
feat(auth): capture refresh_token from mobile signin response
fix(sync): hold cursor behind messages with unconfirmed media
chore(release): prepare v0.4.3 changelog
```

## Pull requests

**Title:** same convention as a commit subject
(e.g. `fix(sync): prevent media loss on interrupted sync`).
Release PRs: `chore(release): vX.Y.Z`.

**Body:** four required sections, in this order:

```markdown
## Why
<!-- Symptom/problem being solved; root cause if a bug. One short paragraph. -->

## What
<!-- Bulleted changes: APIs added/changed in backticks, behavior before/after. -->

## Testing
<!-- Suites run with counts (e.g. "pytest 412 passed · ruff/mypy clean").
     Flag manual-only verification honestly; state what is NOT covered. -->

## Changelog
<!-- Quote the CHANGELOG.md [Unreleased] entry this PR adds, in a fenced block.
     Or the single line: No user-facing change. -->
```

Optional sections, after the required four: `## Depends on` (cross-repo
dependencies, e.g. paired SakaDesk PRs), `## Notes`.

Always pass the body via `--body-file` or a heredoc so tooling can validate
it. Keep the `🤖 Generated with [Claude Code](https://claude.com/claude-code)`
footer when an agent authors the PR.

## CHANGELOG.md

Keep a Changelog skeleton (categories `Added` / `Changed` / `Fixed` /
`Security` / `Deprecated` / `Removed`; `[Unreleased]` on top; compare-links
footer) and Semantic Versioning.

- **Audience: API consumers (developers).** Name the API in backticks
  (`get_messages`, `scan_member_media`); state before/after behavior.
- **Breaking changes:** bold `**Breaking:**` prefix plus a one-line migration
  hint.
- One entry per **user-visible change** (visible to a developer using the
  library), not per commit.
- Entries are added under `[Unreleased]` **in the same PR** as the change
  (quote them in the PR's `## Changelog` section).
- Wrap at ~80 columns.

Example (good):

> **Breaking:** package renamed from `pyhako` to `pysaka`; update imports
> and dependency pins.

## GitHub release notes

Normally automatic: CI's `github-release` job creates the release titled
`pysaka X.Y.Z` with notes pulled from the matching CHANGELOG.md section.
Verify it exists; do not rewrite it.

Manual fallback (only if CI fails to create the release):

```markdown
## Highlights

- **<Developer-visible change>.** <One sentence of detail.>
<!-- 2–5 bullets from the CHANGELOG section. -->

Published to PyPI: https://pypi.org/project/pysaka/X.Y.Z/

**Full Changelog:** [CHANGELOG.md](https://github.com/xebjhm/pysaka/blob/main/CHANGELOG.md#xyz---yyyy-mm-dd)
```
