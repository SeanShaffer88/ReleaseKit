# ReleaseKit — roadmap

## v0.1.0 (this release)

- `plan` / `prepare` / `notes` / `verify` from a single `release.toml`.
- CHANGELOG-section and template notes; version from Cargo.toml /
  pyproject.toml / VERSION file / custom regex / git tag.
- Verify as the ship gate: name-set, size, and digest comparison against
  the manifest, full re-measurement (download + hash) when no manifest
  or no API digest is available.

## Planned

- **In-API release creation with a user-supplied token** — an explicit
  `create` command for users who want one tool end-to-end. Deliberately
  not in v0.1: ReleaseKit's value is that preparation + verification are
  trustworthy even when creation happens elsewhere, and holding
  credentials is a real cost. If it lands, verify still runs after
  create, automatically.
- **winget manifest emission** — generate the version/installer YAML for
  a prepared release so the winget submission stops being a separate
  hand-written step (pairs with the AppHost/installer work).
- **Multi-repo release trains** — one command that plans/prepares/verifies
  a coordinated release across several repos (e.g. WinDevKit kit +
  consumer app), with a combined report and per-repo gates.
- **Attach `SHA256SUMS.txt` itself as a release asset** in the printed
  `gh` command (opt-in flag; some projects publish it, some don't).
- **`verify --watch`** — poll after create until assets finish uploading,
  then verify (large installers can lag the release record).
