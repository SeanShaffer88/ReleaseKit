## TL;DR

ReleaseKit is a zero-dependency Python 3 CLI that turns a hand-driven
GitHub release into one prepared + verified flow: `plan` proves a project
is release-ready, `prepare` builds/collects the assets and writes
`SHA256SUMS.txt`, the rendered notes, and a machine-readable `release.json`,
and `verify` — the ship gate — compares the *published* release against
that manifest (asset name set, sizes, and SHA-256 digests, re-downloading
when the API offers no digest). A release is not "shipped" until `verify`
passes. ReleaseKit holds no credentials and never creates a release
itself; creation stays with `gh` (prepare prints the exact command) or
your existing flow.

## Why

Releases used to be re-derived and executed by hand per project
(WinDevKit PAIN_POINTS #19), and it bit us — all on 2026-10-24:

- Every release (7 suite tools, both Phase-A kits) was drafted by hand in
  the browser.
- An XToolbox v5.3.11 asset size/hash quoted from memory had to be
  **retracted and re-measured**: CI had rebuilt the asset after the first
  quote. Numbers from a create response — or from memory — are not
  measurements.
- Asset sets drifted by hand mid-stream (TrayClick's zip and
  `SHA256SUMS.txt` were dropped from earlier releases manually).

## How a release flows

```
plan  ->  CI green  ->  prepare  ->  create  ->  verify  ->  shipped
```

1. **`plan`** — read-only readiness report from `release.toml`: resolved
   version, tag, title, asset existence/sizes, notes status, and
   best-effort remote checks (tag already on the remote? latest CI run?),
   each labelled best-effort. Exits non-zero with named problems when the
   project is not release-ready.
2. **CI green** — your existing gate; ReleaseKit does not replace it.
3. **`prepare`** — runs asset build commands, collects assets into an out
   dir, writes `SHA256SUMS.txt` (sha256 + size + name per asset),
   `notes.md` (template substitution, or the CHANGELOG section for the
   version — missing section = loud failure), and `release.json`
   (repo, tag, title, per-asset path/sha256/size, and the exact
   `gh release create` command line).
4. **create** — run the printed `gh release create …` command, or create
   the release through your existing flow using the prepared assets.
5. **`verify`** — the gate. Compares the live release against
   `release.json` / `SHA256SUMS.txt`: name-set equality (extra or missing
   asset fails), size equality, and the API's sha256 digest per asset;
   when the API returns no digest for an asset, verify downloads it and
   hashes it itself. With **no manifest at all**, verify downloads and
   re-measures *every* asset and prints the verified table — the full
   re-measurement that caught the stale XToolbox asset. Also checks the
   release is published (not a draft), reports whether it is the Latest
   release, and prints the tag target. Any mismatch: exit non-zero, one
   named reason per asset.

## Usage

```sh
# Config lives at ./release.toml (see examples/). Override with --config.

# 1. Readiness report (read-only; safe any time)
python3 releasekit.py plan
python3 releasekit.py plan --config path/to/release.toml --version 1.2.3

# 2. Render the notes alone (CHANGELOG section or template), e.g. to paste
python3 releasekit.py notes

# 3. Prepare the release pack (assets + SHA256SUMS.txt + notes.md + release.json)
python3 releasekit.py prepare
python3 releasekit.py prepare --out dist/v1.2.3

# 4. Create the release with the command prepare printed, e.g.:
#    gh release create v1.2.3 --repo Owner/Name --title "Name v1.2.3" \
#        --notes-file <out>/notes.md <out>/asset1 <out>/asset2

# 5. Verify before calling it shipped
python3 releasekit.py verify --repo Owner/Name --tag v1.2.3 --release-json <out>/release.json
python3 releasekit.py verify --repo Owner/Name --tag v1.2.3 --manifest <out>/SHA256SUMS.txt
# …or, with no manifest, full re-measurement of whatever is published:
python3 releasekit.py verify --repo Owner/Name --tag v1.2.3
```

Exit codes: 0 = ready / prepared / verified; 1 = named validation or
verify problems; 2 = usage, config, or network errors. Expected failures
print one clean `error:` line — no tracebacks.

`verify` uses the GitHub API anonymously for public repos; set
`GITHUB_TOKEN` to authenticate (higher rate limits, private repos).

## Config: `release.toml`

```toml
[release]
repo = "Owner/Name"          # required
tag_prefix = "v"             # default "v"
notes = "CHANGELOG"          # "CHANGELOG" (extract the version's section
                             # from CHANGELOG.md) or a template file path
title = "{name} {tag}"       # optional pattern

[version]
source = "Cargo.toml"        # file path, or "git-tag"
regex = 'version\s*=\s*"([^"]+)"'   # optional custom extractor

[[asset]]                    # zero or more; omit for source-only releases
name = "App-{version}-x64.exe"     # {version}/{tag} substituted
path = "target/release/app.exe"    # local file (same substitution)
build = "cargo build --release"    # optional; run by prepare
```

Version sources: a `Cargo.toml` / `pyproject.toml` (`[package]`/`version =`
by default), a plain `VERSION` file (first line), any file with a custom
`regex`, or `"git-tag"` (latest tag via `git describe`). `--version`
overrides everywhere.

Notes templates substitute `{version}`, `{tag}`, `{date}`, `{title}`,
`{name}`, and `{repo}`. CHANGELOG mode extracts the section between the
version's `## ` heading and the next heading — and fails loudly if the
section is missing or empty.

Worked examples live in `examples/`: `dll-kit.release.toml`
(ProcessKit-style source-only, CHANGELOG notes) and `app.release.toml`
(TrayClick-style installer + portable exe, template notes).

## Limitations

- **ReleaseKit never publishes.** It holds no credentials by design;
  creation is delegated to `gh` or your existing flow. What it owns is
  preparation and proof.
- **Digest comparison depends on the API response.** When an asset lacks
  a `digest` in the API payload, verify falls back to downloading and
  hashing it — always correct, just slower.
- `plan`'s remote tag / CI checks are best-effort and labelled as such;
  offline, they report "unknown/unavailable" rather than failing the plan.
- Build commands run through the shell in the project directory — only
  put commands in `release.toml` that you would run yourself.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests run entirely on recorded fixtures and temp dirs — no network, no
live GitHub calls (see AGENTS.md).

## License

MIT — see [LICENSE](LICENSE).
