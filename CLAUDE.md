# pysaka — agent instructions

## Documentation formats (MUST follow)

Before writing any of these, read `docs/DOC_FORMATS.md` and follow it exactly:

- **Commit messages** — Conventional Commits: `type(scope)!: subject`,
  imperative, whole line ≤ 72 chars, no trailing period.
- **PR title + body** — title like a commit subject; body sections `## Why`,
  `## What`, `## Testing`, `## Changelog` in that order (all four required).
  Release PRs are titled `chore(release): vX.Y.Z`.
- **CHANGELOG.md** — Keep a Changelog categories; entries written for
  **API consumers** (APIs in backticks, before/after behavior,
  bold `**Breaking:**` prefix + migration hint for breaking changes);
  added under `[Unreleased]` in the same PR.
- **GitHub release notes** — CI-generated from the CHANGELOG section; verify,
  don't rewrite. Manual fallback template lives in `docs/DOC_FORMATS.md`.

A PreToolUse hook on this machine blocks `git commit` / `gh pr create` /
`gh release` calls that violate these formats — if a call is blocked, fix the
content per `docs/DOC_FORMATS.md` and re-run it.
