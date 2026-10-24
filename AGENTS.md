# ReleaseKit — agent notes

- **Verify is the ship gate — load-bearing, not ceremony.** Never report a
  release as shipped from a create response, an upload listing, or a hash
  quoted earlier: CI can rebuild an asset after it was first measured
  (XToolbox v5.3.11's size/hash had to be retracted for exactly this).
  The shipped claim requires `verify` passing against the prepared
  manifest (`release.json` / `SHA256SUMS.txt`), or a full no-manifest
  re-measurement — same evidence, freshly taken.
- ReleaseKit holds **no credentials** and never creates a release. That
  split is the design: prepare + verify stay trustworthy regardless of
  how creation happened. Don't add a create path without SEAN's say-so
  (it's a ROADMAP item with an explicit token story).
- **Stdlib only** (Python 3.11+, tomllib). No dependencies, ever — if a
  feature seems to need one, it needs a smaller design instead.
- Tests use **recorded fixtures only** (`tests/fixtures/api/*.json`,
  shaped from real API responses) plus temp dirs. No live GitHub calls in
  the suite: network behaviour is isolated behind `fetch_json` /
  `fetch_bytes` / the `downloader` parameter of `compare_release`.
- Expected failures are `RKError` → one clean `error:` line and a named
  exit code (1 = validation/verify problems, 2 = usage/config/network).
  A traceback in normal operation is a bug.
- `parse_sums` accepts both ReleaseKit's 3-field lines
  (`sha256  size  name`) and plain `sha256sum` 2-field output; keep that
  tolerance — manifests in the wild come from both.
- Match PublishPilot conventions for CLI shape, output style, exit
  codes, and README voice when extending commands.
