#!/usr/bin/env python3
"""ReleaseKit — one prepared + verified flow for GitHub releases.

Stdlib only, Python 3.11+ (tomllib). Holds NO credentials and never creates
a release itself: it plans, prepares (assets + SHA256SUMS.txt + notes +
release.json), and — the load-bearing part — VERIFIES a published release
by comparing the live GitHub API state (and, when needed, a full
re-download) against the prepared manifest.

Incidents encoded here (all 2026-10-24):
- Releases were drafted by hand in the browser, one at a time, per project.
- An asset size/hash was quoted from memory for XToolbox v5.3.11 and had
  to be retracted: CI had rebuilt the asset after the first quote. Only a
  fresh re-measurement is trustworthy — verify re-measures.
- Asset sets drifted mid-stream by hand (TrayClick dropped its zip and
  SHA256SUMS.txt from earlier releases manually). The manifest is the
  contract; verify enforces name-set equality against it.

A release is not "shipped" until `verify` passes against the manifest.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = "https://api.github.com"
TOOL_VERSION = "0.1.0"


class RKError(Exception):
    """An expected failure mode: clean message, no traceback."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def substitute(text: str, values: dict[str, str]) -> str:
    """Replace {key} placeholders; unknown placeholders are left alone."""
    for key, val in values.items():
        text = text.replace("{" + key + "}", val)
    return text


def run_quiet(cmd: list[str], cwd: Path | None = None, timeout: int = 20) -> str | None:
    """Run a command, return stdout on success, None on any failure."""
    try:
        out = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                             capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(config_path: Path) -> tuple[dict, Path]:
    """Load + validate release.toml. Returns (config dict, project dir)."""
    if not config_path.is_file():
        raise RKError(f"config not found: {config_path} "
                      f"(expected a release.toml — see examples/)")
    try:
        with config_path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise RKError(f"{config_path}: TOML parse error: {exc}")
    except OSError as exc:
        raise RKError(f"cannot read config {config_path}: {exc}")

    project_dir = config_path.resolve().parent
    release = raw.get("release")
    if not isinstance(release, dict):
        raise RKError(f"{config_path}: missing [release] table")
    repo = release.get("repo")
    if not repo or not isinstance(repo, str) or "/" not in repo:
        raise RKError(f"{config_path}: [release] repo is required and must "
                      f"look like \"Owner/Name\" (got {repo!r})")
    version_tbl = raw.get("version")
    if not isinstance(version_tbl, dict) or not version_tbl.get("source"):
        raise RKError(f"{config_path}: [version] source is required "
                      f"(a file path, or \"git-tag\")")
    assets = raw.get("asset", [])
    if not isinstance(assets, list):
        raise RKError(f"{config_path}: [[asset]] entries must be tables")
    for i, asset in enumerate(assets):
        if not isinstance(asset, dict) or not asset.get("name") or not asset.get("path"):
            raise RKError(f"{config_path}: [[asset]] entry #{i + 1} needs "
                          f"both name and path")
    cfg = {
        "repo": repo,
        "tag_prefix": release.get("tag_prefix", "v"),
        "notes": release.get("notes", "CHANGELOG"),
        "title": release.get("title"),
        "version_source": version_tbl["source"],
        "version_regex": version_tbl.get("regex"),
        "assets": assets,
    }
    return cfg, project_dir


# ---------------------------------------------------------------------------
# Version resolution
# ---------------------------------------------------------------------------

_PACKAGE_VERSION_RE = re.compile(
    r"(?ms)^\[package\].*?^\s*version\s*=\s*\"([^\"]+)\"")
_GENERIC_VERSION_RE = re.compile(r"(?m)^\s*version\s*=\s*\"([^\"]+)\"")


def resolve_version(cfg: dict, project_dir: Path, override: str | None) -> str:
    if override:
        return override.strip()
    source = cfg["version_source"]
    prefix = cfg["tag_prefix"]
    if source == "git-tag":
        out = run_quiet(["git", "describe", "--tags", "--abbrev=0"], cwd=project_dir)
        if not out or not out.strip():
            raise RKError("version source is \"git-tag\" but `git describe "
                          "--tags` found no tag (pass --version to override)")
        version = out.strip()
    else:
        path = project_dir / source
        if not path.is_file():
            raise RKError(f"version source file not found: {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        regex = cfg.get("version_regex")
        if regex:
            m = re.search(regex, text, re.MULTILINE)
            if not m:
                raise RKError(f"version regex {regex!r} did not match in {path} "
                              f"(check [version] regex in release.toml)")
            version = m.group(1).strip()
        elif path.suffix == ".toml":
            m = _PACKAGE_VERSION_RE.search(text) or _GENERIC_VERSION_RE.search(text)
            if not m:
                raise RKError(f"no version = \"...\" found in {path} "
                              f"(add [version] regex to release.toml)")
            version = m.group(1).strip()
        else:
            line = next((ln.strip() for ln in text.splitlines()
                         if ln.strip() and not ln.strip().startswith("#")), "")
            if not line:
                raise RKError(f"version file {path} is empty")
            version = line.split()[0]
    if prefix and version.startswith(prefix) and re.match(r"\d", version[len(prefix):]):
        version = version[len(prefix):]
    if not re.match(r"^\d+\.\d+\.\d+", version):
        raise RKError(f"resolved version {version!r} does not look like "
                      f"X.Y.Z (from source {source!r})", code=2)
    return version


def tag_for(cfg: dict, version: str) -> str:
    return f"{cfg['tag_prefix']}{version}"


def title_for(cfg: dict, version: str, tag: str) -> str:
    name = cfg["repo"].split("/", 1)[1]
    pattern = cfg.get("title") or "{name} {tag}"
    return substitute(pattern, {"name": name, "version": version, "tag": tag,
                                "repo": cfg["repo"]})


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^##\s+(.*\S)\s*$")
_HEADING_VERSION_RE = re.compile(r"^\[?v?(\d+\.\d+\.\d+[^\s\]]*)")


def extract_changelog_section(text: str, version: str) -> str:
    """Return the CHANGELOG section for *version*.

    The section runs from its `## <version>` heading to the next heading
    of level <= 2 (or EOF). Fails loudly when the section is missing or
    empty — a release without notes is a named problem, not a surprise.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            vm = _HEADING_VERSION_RE.match(m.group(1))
            if vm and vm.group(1) == version:
                start = i
                break
    if start is None:
        raise RKError(f"CHANGELOG has no section for version {version} "
                      f"(looked for a '## {version}' heading) — add the "
                      f"section before releasing", code=1)
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^#{1,2}\s", lines[j]):
            end = j
            break
    section = "\n".join(lines[start:end]).strip() + "\n"
    body = "\n".join(lines[start + 1:end]).strip()
    if not body:
        raise RKError(f"CHANGELOG section for {version} is empty — write "
                      f"the notes before releasing", code=1)
    return section


def render_notes(cfg: dict, project_dir: Path, version: str, tag: str,
                 title: str) -> str:
    notes_spec = str(cfg.get("notes", "CHANGELOG"))
    values = {"version": version, "tag": tag, "title": title,
              "name": cfg["repo"].split("/", 1)[1],
              "repo": cfg["repo"], "date": datetime.date.today().isoformat()}
    if notes_spec.upper() == "CHANGELOG" or notes_spec.upper().startswith("CHANGELOG"):
        rel = "CHANGELOG.md" if notes_spec.upper() == "CHANGELOG" else notes_spec
        path = project_dir / rel
        if not path.is_file():
            raise RKError(f"notes mode is CHANGELOG but {path} does not exist",
                          code=1)
        return extract_changelog_section(
            path.read_text(encoding="utf-8", errors="replace"), version)
    path = project_dir / notes_spec
    if not path.is_file():
        raise RKError(f"notes template not found: {path}", code=1)
    return substitute(path.read_text(encoding="utf-8"), values)


# ---------------------------------------------------------------------------
# Assets + manifest
# ---------------------------------------------------------------------------

def asset_name(asset: dict, version: str, tag: str) -> str:
    return substitute(str(asset["name"]), {"version": version, "tag": tag})


def asset_source(asset: dict, project_dir: Path, version: str, tag: str) -> Path:
    rel = substitute(str(asset["path"]), {"version": version, "tag": tag})
    p = Path(rel)
    return p if p.is_absolute() else project_dir / p


def build_sums(assets: list[dict], repo: str, tag: str) -> str:
    """SHA256SUMS.txt text: one '<sha256>  <size>  <name>' line per asset."""
    lines = [f"# ReleaseKit SHA256SUMS for {repo} {tag}"]
    for a in assets:
        lines.append(f"{a['sha256']}  {a['size']}  {a['name']}")
    return "\n".join(lines) + "\n"


_SUMS_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})\s+(?:(\d+)\s+)?\*?(\S.*)$")


def parse_sums(text: str) -> list[dict]:
    """Parse SHA256SUMS.txt (ReleaseKit 3-field or sha256sum 2-field lines)."""
    entries = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _SUMS_LINE_RE.match(line)
        if not m:
            raise RKError(f"SHA256SUMS line {lineno} is not "
                          f"'<sha256>  [<size>]  <name>': {raw!r}")
        entries.append({
            "name": m.group(3),
            "sha256": m.group(1).lower(),
            "size": int(m.group(2)) if m.group(2) is not None else None,
        })
    if not entries:
        raise RKError("SHA256SUMS manifest contains no assets")
    return entries


# ---------------------------------------------------------------------------
# GitHub API (verify / plan best-effort)
# ---------------------------------------------------------------------------

def api_headers() -> dict:
    headers = {
        "User-Agent": f"releasekit/{TOOL_VERSION}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers=api_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RKError(f"GitHub API {exc.code} for {url} — "
                      f"{exc.reason or 'request failed'}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RKError(f"cannot reach the GitHub API ({url}): {exc}")


def fetch_bytes(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers=api_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise RKError(f"download failed: HTTP {exc.code} for {url}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RKError(f"cannot download {url}: {exc}")


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

def remote_tag_exists(project_dir: Path, tag: str) -> str:
    out = run_quiet(["git", "ls-remote", "--tags", "origin",
                     f"refs/tags/{tag}"], cwd=project_dir, timeout=15)
    if out is None:
        return "unknown (git ls-remote failed — offline or no origin)"
    return "yes" if out.strip() else "no"


def latest_ci_status(repo: str) -> str:
    try:
        data = fetch_json(f"{API_BASE}/repos/{repo}/actions/runs?per_page=1",
                          timeout=10)
    except RKError:
        return "unavailable (API unreachable)"
    runs = data.get("workflow_runs") or []
    if not runs:
        return "no workflow runs found"
    run = runs[0]
    bits = [run.get("name") or "workflow", run.get("status") or "?"]
    if run.get("conclusion"):
        bits.append(run["conclusion"])
    if run.get("head_branch"):
        bits.append(f"on {run['head_branch']}")
    return " ".join(bits)


def cmd_plan(args: argparse.Namespace) -> int:
    cfg, project_dir = load_config(Path(args.config))
    version = resolve_version(cfg, project_dir, args.version)
    tag = tag_for(cfg, version)
    title = title_for(cfg, version, tag)
    problems: list[str] = []

    print(f"Release plan for {cfg['repo']}")
    print(f"  version : {version}")
    print(f"  tag     : {tag}")
    print(f"  title   : {title}")

    print("  assets  :")
    if not cfg["assets"]:
        print("    (none — source-only release)")
    for asset in cfg["assets"]:
        name = asset_name(asset, version, tag)
        src = asset_source(asset, project_dir, version, tag)
        if src.is_file():
            print(f"    {name}  <- {src} ({src.stat().st_size} bytes)")
        elif asset.get("build"):
            print(f"    {name}  <- {src} (missing; build: {asset['build']})")
        else:
            problems.append(f"asset {name}: source {src} does not exist "
                            f"and no build command is configured")
            print(f"    {name}  <- {src} (MISSING, no build command)")

    try:
        notes = render_notes(cfg, project_dir, version, tag, title)
        first = notes.splitlines()[0] if notes.splitlines() else ""
        print(f"  notes   : ok ({len(notes)} chars; starts: {first!r})")
    except RKError as exc:
        problems.append(str(exc))
        print(f"  notes   : PROBLEM — {exc}")

    print(f"  tag on remote : {remote_tag_exists(project_dir, tag)} (best-effort)")
    print(f"  latest CI     : {latest_ci_status(cfg['repo'])} (best-effort)")

    if problems:
        print("\nNot release-ready:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nPlan is release-ready.")
    return 0


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

def cmd_prepare(args: argparse.Namespace) -> int:
    cfg, project_dir = load_config(Path(args.config))
    version = resolve_version(cfg, project_dir, args.version)
    tag = tag_for(cfg, version)
    title = title_for(cfg, version, tag)

    out_dir = Path(args.out).resolve() if args.out else \
        project_dir / "release-out" / tag
    out_dir.mkdir(parents=True, exist_ok=True)

    collected: list[dict] = []
    for asset in cfg["assets"]:
        name = asset_name(asset, version, tag)
        src = asset_source(asset, project_dir, version, tag)
        build = asset.get("build")
        if build:
            print(f"building {name}: {build}")
            try:
                proc = subprocess.run(build, shell=True, cwd=str(project_dir))
            except OSError as exc:
                raise RKError(f"build command for asset {name} could not "
                              f"start: {exc}")
            if proc.returncode != 0:
                raise RKError(f"build command for asset {name} failed "
                              f"(exit {proc.returncode}): {build}", code=1)
        if not src.is_file():
            raise RKError(f"asset {name}: source file {src} does not exist"
                          + (" (build command ran but produced nothing)"
                             if build else " (and no build command is set)"),
                          code=1)
        dest = out_dir / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        data_size = dest.stat().st_size
        collected.append({
            "name": name,
            "path": str(dest),
            "sha256": sha256_file(dest),
            "size": data_size,
        })
        print(f"collected {name} ({data_size} bytes)")

    notes_text = render_notes(cfg, project_dir, version, tag, title)
    notes_path = out_dir / "notes.md"
    notes_path.write_text(notes_text, encoding="utf-8", newline="\n")

    sums_path = out_dir / "SHA256SUMS.txt"
    sums_path.write_text(build_sums(collected, cfg["repo"], tag),
                         encoding="utf-8", newline="\n")

    head = run_quiet(["git", "rev-parse", "HEAD"], cwd=project_dir)
    gh_parts = ["gh", "release", "create", tag, "--repo", cfg["repo"],
                "--title", title, "--notes-file", str(notes_path)]
    gh_parts += [a["path"] for a in collected]
    pack = {
        "version": 1,
        "tool": "releasekit",
        "repo": cfg["repo"],
        "tag": tag,
        "title": title,
        "release_version": version,
        "date": datetime.date.today().isoformat(),
        "target": head.strip() if head else None,
        "notes_file": str(notes_path),
        "sums_file": str(sums_path),
        "assets": collected,
        "gh_command": shlex.join(gh_parts),
    }
    pack_path = out_dir / "release.json"
    pack_path.write_text(json.dumps(pack, indent=2) + "\n",
                         encoding="utf-8", newline="\n")

    print(f"\nwrote {notes_path}")
    print(f"wrote {sums_path}")
    print(f"wrote {pack_path}")
    print("\nReleaseKit does NOT create the release. Create it with:")
    print(f"  {pack['gh_command']}")
    print("Then verify it before calling it shipped:")
    print(f"  python3 releasekit.py verify --repo {cfg['repo']} --tag {tag} "
          f"--release-json {pack_path}")
    return 0


# ---------------------------------------------------------------------------
# notes
# ---------------------------------------------------------------------------

def cmd_notes(args: argparse.Namespace) -> int:
    cfg, project_dir = load_config(Path(args.config))
    version = resolve_version(cfg, project_dir, args.version)
    tag = tag_for(cfg, version)
    title = title_for(cfg, version, tag)
    sys.stdout.write(render_notes(cfg, project_dir, version, tag, title))
    return 0


# ---------------------------------------------------------------------------
# verify — the ship gate
# ---------------------------------------------------------------------------

def compare_release(release: dict, latest: dict | None,
                    expected: list[dict] | None,
                    downloader=fetch_bytes) -> tuple[list[str], list[dict], list[str]]:
    """Compare a live release payload against the manifest.

    Returns (problems, rows, infos). Pure given a downloader, so tests run
    on recorded fixtures with no network.
    """
    problems: list[str] = []
    infos: list[str] = []
    rows: list[dict] = []
    tag = release.get("tag_name", "?")

    if release.get("draft"):
        problems.append(f"release {tag} is a DRAFT — not published")
    if release.get("prerelease"):
        infos.append(f"release {tag} is marked pre-release")
    infos.append(f"tag target: {release.get('target_commitish')}")

    if latest is not None:
        if latest.get("tag_name") == tag:
            infos.append("this release is the current Latest")
        else:
            infos.append(f"not the Latest release (latest is "
                         f"{latest.get('tag_name')}) — informational only")

    remote = {a["name"]: a for a in release.get("assets", [])}

    if expected is not None:
        expected_by_name = {e["name"]: e for e in expected}
        for name in sorted(expected_by_name):
            if name not in remote:
                problems.append(f"asset missing on GitHub: {name}")
        for name in sorted(remote):
            if name not in expected_by_name:
                problems.append(f"unexpected asset on GitHub: {name} "
                                f"(not in the manifest)")
        iterable = [(n, expected_by_name[n], remote[n])
                    for n in sorted(expected_by_name) if n in remote]
    else:
        iterable = [(n, None, remote[n]) for n in sorted(remote)]

    for name, exp, asset in iterable:
        row = {"name": name, "size": asset.get("size"),
               "sha256": None, "result": "ok"}
        digest = asset.get("digest") or ""
        api_sha = digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None
        if exp is not None:
            if exp.get("size") is not None and exp["size"] != asset.get("size"):
                problems.append(
                    f"asset {name}: size mismatch — manifest "
                    f"{exp['size']} bytes, GitHub {asset.get('size')} bytes")
                row["result"] = "SIZE MISMATCH"
            if exp.get("sha256"):
                if api_sha:
                    row["sha256"] = api_sha
                    if api_sha != exp["sha256"]:
                        problems.append(
                            f"asset {name}: sha256 mismatch — manifest "
                            f"{exp['sha256']}, GitHub digest {api_sha}")
                        row["result"] = "SHA256 MISMATCH"
                else:
                    infos.append(f"asset {name}: no digest in API response — "
                                 f"re-downloading to hash")
                    data = downloader(asset["browser_download_url"])
                    got = sha256_hex(data)
                    row["sha256"] = got
                    row["size"] = len(data)
                    if got != exp["sha256"]:
                        problems.append(
                            f"asset {name}: sha256 mismatch — manifest "
                            f"{exp['sha256']}, re-downloaded {got}")
                        row["result"] = "SHA256 MISMATCH"
        else:
            # No manifest: full re-measurement of every asset.
            data = downloader(asset["browser_download_url"])
            row["sha256"] = sha256_hex(data)
            row["size"] = len(data)
            if len(data) != asset.get("size"):
                problems.append(
                    f"asset {name}: API reports {asset.get('size')} bytes "
                    f"but the download is {len(data)} bytes — the asset "
                    f"may have been rebuilt after upload")
                row["result"] = "SIZE MISMATCH"
        rows.append(row)
    return problems, rows, infos


def load_expected(args: argparse.Namespace) -> list[dict] | None:
    if args.release_json:
        try:
            pack = json.loads(Path(args.release_json).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RKError(f"cannot read release.json: {exc}")
        return [{"name": a["name"], "sha256": a.get("sha256"),
                 "size": a.get("size")} for a in pack.get("assets", [])]
    if args.manifest:
        try:
            text = Path(args.manifest).read_text(encoding="utf-8")
        except OSError as exc:
            raise RKError(f"cannot read manifest: {exc}")
        return parse_sums(text)
    return None


def cmd_verify(args: argparse.Namespace) -> int:
    repo = args.repo
    if "/" not in repo:
        raise RKError(f"--repo must look like \"Owner/Name\" (got {repo!r})")
    expected = load_expected(args)
    release = fetch_json(f"{API_BASE}/repos/{repo}/releases/tags/{args.tag}")
    try:
        latest = fetch_json(f"{API_BASE}/repos/{repo}/releases/latest")
    except RKError:
        latest = None  # no published releases at all, or unreachable

    problems, rows, infos = compare_release(release, latest, expected)
    for info in infos:
        print(f"info: {info}")
    if rows:
        print(f"\n{'Asset':<44} {'Bytes':>10}  SHA-256 / Result")
        print("-" * 100)
        for row in rows:
            sha = row["sha256"] or "(from API size only)"
            print(f"{row['name']:<44} {row['size']:>10}  {sha} [{row['result']}]")
    elif expected:
        print("\n(no assets compared)")
    else:
        print("\nrelease has no assets (source-only) — nothing to measure")
    if problems:
        print("\nVERIFY FAILED:")
        for p in problems:
            print(f"  - {p}")
        print("Do not call this release shipped until verify passes.")
        return 1
    print(f"\nVERIFIED: {repo} {args.tag} matches "
          + ("the manifest." if expected is not None
             else "its API record (full re-measurement)."))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="releasekit",
        description="Prepare and verify GitHub releases from a release.toml: "
                    "plan the release, prepare assets + SHA256SUMS + notes, "
                    "and verify the published release against the manifest "
                    "before calling it shipped. ReleaseKit holds no "
                    "credentials and never creates a release itself.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="validate config and report release readiness (read-only)")
    p_plan.add_argument("--config", default="release.toml", help="config file (default: release.toml)")
    p_plan.add_argument("--version", default=None, help="override the resolved version")
    p_plan.set_defaults(func=cmd_plan)

    p_prep = sub.add_parser("prepare", help="build/collect assets, SHA256SUMS.txt, notes.md, release.json")
    p_prep.add_argument("--config", default="release.toml", help="config file (default: release.toml)")
    p_prep.add_argument("--version", default=None, help="override the resolved version")
    p_prep.add_argument("--out", default=None, help="output dir (default: <project>/release-out/<tag>)")
    p_prep.set_defaults(func=cmd_prepare)

    p_notes = sub.add_parser("notes", help="print the rendered release notes")
    p_notes.add_argument("--config", default="release.toml", help="config file (default: release.toml)")
    p_notes.add_argument("--version", default=None, help="override the resolved version")
    p_notes.set_defaults(func=cmd_notes)

    p_ver = sub.add_parser("verify", help="verify a published release against the manifest (the ship gate)")
    p_ver.add_argument("--repo", required=True, help="Owner/Name")
    p_ver.add_argument("--tag", required=True, help="release tag, e.g. v0.1.0")
    p_ver.add_argument("--manifest", default=None, help="SHA256SUMS.txt from prepare")
    p_ver.add_argument("--release-json", default=None, help="release.json from prepare")
    p_ver.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RKError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except BrokenPipeError:  # pragma: no cover
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
