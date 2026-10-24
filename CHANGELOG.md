# Changelog

## 0.1.0 — 2026-10-24

- Initial release (WinDevKit Phase B #1, PAIN_POINTS #19).
- `plan` subcommand: read-only release-readiness report from
  `release.toml` — version/tag/title resolution, asset existence and
  sizes, notes status, best-effort remote tag and CI checks.
- `prepare` subcommand: asset build commands + collection,
  `SHA256SUMS.txt`, rendered `notes.md`, and `release.json` with the
  exact `gh release create` command. Never creates a release itself.
- `notes` subcommand: CHANGELOG-section extraction (loud failure when
  the version's section is missing) or template rendering with
  {version}/{tag}/{date}/{title}/{name}/{repo} substitution.
- `verify` subcommand — the ship gate: published-vs-draft check,
  asset name-set equality, size equality, API sha256 digest comparison,
  download-and-hash fallback, and full re-measurement with no manifest.
- Version sources: Cargo.toml / pyproject.toml, VERSION file, custom
  regex, or git tag; `--version` override everywhere.
- Test suite (31 tests) on recorded GitHub API fixtures and temp-dir
  projects — no live network calls.
