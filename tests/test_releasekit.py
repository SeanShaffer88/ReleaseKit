import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import releasekit as rk

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture_json(rel):
    return json.loads((FIXTURES / rel).read_text(encoding="utf-8"))


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, text: str):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
        return p


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestConfig(TempDirCase):
    def test_parse_ok(self):
        cfg, project_dir = rk.load_config(FIXTURES / "cargo" / "release.toml")
        self.assertEqual(cfg["repo"], "SeanShaffer88/FixtureKit")
        self.assertEqual(cfg["tag_prefix"], "v")
        self.assertEqual(cfg["version_source"], "Cargo.toml")
        self.assertEqual(project_dir, (FIXTURES / "cargo").resolve())

    def test_missing_config_file(self):
        with self.assertRaises(rk.RKError) as cm:
            rk.load_config(self.root / "nope.toml")
        self.assertIn("config not found", str(cm.exception))

    def test_missing_repo_named(self):
        p = self.write("release.toml", '[version]\nsource = "VERSION"\n')
        with self.assertRaises(rk.RKError) as cm:
            rk.load_config(p)
        self.assertIn("[release]", str(cm.exception))

    def test_bad_repo_named(self):
        p = self.write("release.toml",
                       '[release]\nrepo = "NoSlash"\n'
                       '[version]\nsource = "VERSION"\n')
        with self.assertRaises(rk.RKError) as cm:
            rk.load_config(p)
        self.assertIn("repo", str(cm.exception))

    def test_missing_version_source_named(self):
        p = self.write("release.toml", '[release]\nrepo = "A/B"\n')
        with self.assertRaises(rk.RKError) as cm:
            rk.load_config(p)
        self.assertIn("source", str(cm.exception))


# ---------------------------------------------------------------------------
# Version resolution
# ---------------------------------------------------------------------------

class TestVersion(unittest.TestCase):
    def test_from_cargo_toml(self):
        cfg, project_dir = rk.load_config(FIXTURES / "cargo" / "release.toml")
        self.assertEqual(rk.resolve_version(cfg, project_dir, None), "1.2.3")

    def test_from_version_file(self):
        cfg, project_dir = rk.load_config(FIXTURES / "versionfile" / "release.toml")
        self.assertEqual(rk.resolve_version(cfg, project_dir, None), "2.0.1")

    def test_override_wins(self):
        cfg, project_dir = rk.load_config(FIXTURES / "cargo" / "release.toml")
        self.assertEqual(rk.resolve_version(cfg, project_dir, "9.9.9"), "9.9.9")

    def test_custom_regex(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "build.txt").write_text("APP_VERSION=3.4.5-rc1\n",
                                            encoding="utf-8")
            cfg = {"version_source": "build.txt", "tag_prefix": "v",
                   "version_regex": r"APP_VERSION=(\S+)"}
            self.assertEqual(rk.resolve_version(cfg, root, None), "3.4.5-rc1")

    def test_regex_no_match_is_named_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "build.txt").write_text("nothing here\n", encoding="utf-8")
            cfg = {"version_source": "build.txt", "tag_prefix": "v",
                   "version_regex": r"APP_VERSION=(\S+)"}
            with self.assertRaises(rk.RKError) as cm:
                rk.resolve_version(cfg, root, None)
            self.assertIn("did not match", str(cm.exception))

    def test_tag_prefix_stripped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "VERSION").write_text("v4.5.6\n", encoding="utf-8")
            cfg = {"version_source": "VERSION", "tag_prefix": "v",
                   "version_regex": None}
            self.assertEqual(rk.resolve_version(cfg, root, None), "4.5.6")


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

class TestNotes(unittest.TestCase):
    def test_changelog_extraction(self):
        text = (FIXTURES / "cargo" / "CHANGELOG.md").read_text(encoding="utf-8")
        section = rk.extract_changelog_section(text, "1.2.3")
        self.assertIn("Fixed the thing.", section)
        self.assertNotIn("1.2.2", section)

    def test_changelog_last_section_edge(self):
        text = (FIXTURES / "cargo" / "CHANGELOG.md").read_text(encoding="utf-8")
        section = rk.extract_changelog_section(text, "1.2.2")
        self.assertIn("Older notes, last section", section)

    def test_changelog_missing_fails_loudly(self):
        text = (FIXTURES / "cargo" / "CHANGELOG.md").read_text(encoding="utf-8")
        with self.assertRaises(rk.RKError) as cm:
            rk.extract_changelog_section(text, "9.9.9")
        self.assertIn("no section for version 9.9.9", str(cm.exception))

    def test_template_substitution(self):
        cfg, project_dir = rk.load_config(FIXTURES / "cargo" / "release.toml")
        cfg = dict(cfg, notes="../template/notes.md")
        out = rk.render_notes(cfg, project_dir, "1.2.3", "v1.2.3",
                              "FixtureKit v1.2.3")
        self.assertIn("version 1.2.3 of SeanShaffer88/FixtureKit", out)
        self.assertIn("FixtureKit v1.2.3", out)
        self.assertNotIn("{version}", out)
        self.assertNotIn("{tag}", out)

    def test_notes_command_end_to_end(self):
        import io
        from contextlib import redirect_stdout
        cfg_path = FIXTURES / "cargo" / "release.toml"
        args = type("A", (), {"config": str(cfg_path), "version": None})()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = rk.cmd_notes(args)
        self.assertEqual(rc, 0)
        self.assertIn("Fixed the thing.", buf.getvalue())


# ---------------------------------------------------------------------------
# Manifest (SHA256SUMS)
# ---------------------------------------------------------------------------

class TestSums(unittest.TestCase):
    def test_sums_match_sha256sum_values(self):
        assets = []
        for name, data in [("a.bin", b"alpha-bytes"), ("b.bin", b"beta")]:
            assets.append({"name": name, "size": len(data),
                           "sha256": hashlib.sha256(data).hexdigest()})
        text = rk.build_sums(assets, "SeanShaffer88/FixtureKit", "v1.2.3")
        lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
        self.assertEqual(len(lines), 2)
        parsed = rk.parse_sums(text)
        self.assertEqual(parsed[0]["sha256"],
                         hashlib.sha256(b"alpha-bytes").hexdigest())
        self.assertEqual(parsed[0]["size"], 11)
        self.assertEqual(parsed[1]["name"], "b.bin")

    def test_parse_two_field_sha256sum_format(self):
        digest = hashlib.sha256(b"x").hexdigest()
        parsed = rk.parse_sums(f"{digest}  x.bin\n")
        self.assertEqual(parsed[0]["name"], "x.bin")
        self.assertIsNone(parsed[0]["size"])

    def test_parse_rejects_garbage(self):
        with self.assertRaises(rk.RKError):
            rk.parse_sums("not a sums file\n")


# ---------------------------------------------------------------------------
# Verify (recorded fixtures, no network)
# ---------------------------------------------------------------------------

def never_download(url):  # pragma: no cover - must not be called
    raise AssertionError(f"downloader should not be called, got {url}")


class TestVerify(unittest.TestCase):
    def setUp(self):
        self.app = load_fixture_json("api/app_release.json")
        self.expected = [
            {"name": a["name"], "size": a["size"],
             "sha256": a["digest"].split(":", 1)[1]}
            for a in self.app["assets"]
        ]

    def test_jobkit_source_only_release_passes(self):
        release = load_fixture_json("api/jobkit_release.json")
        problems, rows, infos = rk.compare_release(release, release, [],
                                                   downloader=never_download)
        self.assertEqual(problems, [])
        self.assertEqual(rows, [])
        self.assertTrue(any("Latest" in i for i in infos))

    def test_matching_assets_pass(self):
        problems, rows, _ = rk.compare_release(
            self.app, self.app, self.expected, downloader=never_download)
        self.assertEqual(problems, [])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["result"] == "ok" for r in rows))

    def test_size_mismatch_fails_named(self):
        bad = copy.deepcopy(self.expected)
        bad[0]["size"] += 1
        problems, _, _ = rk.compare_release(
            self.app, self.app, bad, downloader=never_download)
        self.assertTrue(any("size mismatch" in p and bad[0]["name"] in p
                            for p in problems))

    def test_digest_mismatch_fails_named(self):
        bad = copy.deepcopy(self.expected)
        bad[1]["sha256"] = "0" * 64
        problems, _, _ = rk.compare_release(
            self.app, self.app, bad, downloader=never_download)
        self.assertTrue(any("sha256 mismatch" in p and bad[1]["name"] in p
                            for p in problems))

    def test_extra_asset_fails(self):
        short = self.expected[:1]
        problems, _, _ = rk.compare_release(
            self.app, self.app, short, downloader=never_download)
        self.assertTrue(any("unexpected asset" in p for p in problems))

    def test_missing_asset_fails(self):
        long = self.expected + [{"name": "ghost.exe", "size": 1,
                                 "sha256": "a" * 64}]
        problems, _, _ = rk.compare_release(
            self.app, self.app, long, downloader=never_download)
        self.assertTrue(any("missing on GitHub: ghost.exe" in p
                            for p in problems))

    def test_draft_fails(self):
        draft = copy.deepcopy(self.app)
        draft["draft"] = True
        problems, _, _ = rk.compare_release(
            draft, draft, self.expected, downloader=never_download)
        self.assertTrue(any("DRAFT" in p for p in problems))

    def test_missing_digest_falls_back_to_download(self):
        nodigest = copy.deepcopy(self.app)
        for a in nodigest["assets"]:
            a.pop("digest", None)
        payloads = {a["browser_download_url"]: b"fake-installer-bytes-for-fixture"
                    if "Setup" in a["name"] else b"fake-portable-exe-bytes"
                    for a in nodigest["assets"]}
        problems, rows, infos = rk.compare_release(
            nodigest, nodigest, self.expected,
            downloader=lambda url: payloads[url])
        self.assertEqual(problems, [])
        self.assertTrue(any("re-downloading" in i for i in infos))

    def test_no_manifest_remeasures_downloads(self):
        payloads = {a["browser_download_url"]: b"fake-installer-bytes-for-fixture"
                    if "Setup" in a["name"] else b"fake-portable-exe-bytes"
                    for a in self.app["assets"]}
        problems, rows, _ = rk.compare_release(
            self.app, self.app, None, downloader=lambda url: payloads[url])
        self.assertEqual(problems, [])
        self.assertEqual(rows[0]["sha256"],
                         hashlib.sha256(b"fake-installer-bytes-for-fixture").hexdigest())


# ---------------------------------------------------------------------------
# prepare end-to-end + plan readiness
# ---------------------------------------------------------------------------

class TestPrepare(TempDirCase):
    def _project(self):
        self.write("Cargo.toml",
                   '[package]\nname = "demo"\nversion = "0.3.0"\n')
        self.write("CHANGELOG.md",
                   "# Changelog\n\n## 0.3.0 — 2026-10-24\n\n- Demo release.\n")
        build_cmd = (f'{sys.executable} -c '
                     f'"open(\'app.bin\',\'wb\').write(b\'built-bytes\')"')
        self.write("release.toml",
                   '[release]\nrepo = "SeanShaffer88/Demo"\nnotes = "CHANGELOG"\n'
                   '[version]\nsource = "Cargo.toml"\n'
                   '[[asset]]\nname = "Demo-{version}.bin"\n'
                   f'path = "app.bin"\nbuild = {json.dumps(build_cmd)}\n')
        return self.root / "release.toml"

    def test_prepare_end_to_end(self):
        config = self._project()
        out = self.root / "out"
        args = type("A", (), {"config": str(config), "version": None,
                              "out": str(out)})()
        rc = rk.cmd_prepare(args)
        self.assertEqual(rc, 0)
        pack = json.loads((out / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(pack["tag"], "v0.3.0")
        self.assertEqual(pack["repo"], "SeanShaffer88/Demo")
        self.assertEqual(len(pack["assets"]), 1)
        asset = pack["assets"][0]
        self.assertEqual(asset["name"], "Demo-0.3.0.bin")
        self.assertEqual(asset["sha256"],
                         hashlib.sha256(b"built-bytes").hexdigest())
        self.assertEqual(asset["size"], len(b"built-bytes"))
        self.assertIn("gh release create v0.3.0", pack["gh_command"])
        sums = rk.parse_sums((out / "SHA256SUMS.txt").read_text(encoding="utf-8"))
        self.assertEqual(sums[0]["sha256"], asset["sha256"])
        notes = (out / "notes.md").read_text(encoding="utf-8")
        self.assertIn("Demo release.", notes)
        self.assertTrue((out / "Demo-0.3.0.bin").is_file())

    def test_prepare_missing_asset_fails_loudly(self):
        self.write("VERSION", "1.0.0\n")
        self.write("CHANGELOG.md", "# Changelog\n\n## 1.0.0\n\n- x\n")
        config = self.write("release.toml",
                            '[release]\nrepo = "A/B"\n[version]\n'
                            'source = "VERSION"\n[[asset]]\n'
                            'name = "x.bin"\npath = "nope.bin"\n')
        args = type("A", (), {"config": str(config), "version": None,
                              "out": str(self.root / "out")})()
        with self.assertRaises(rk.RKError) as cm:
            rk.cmd_prepare(args)
        self.assertIn("does not exist", str(cm.exception))

    def test_plan_flags_missing_asset(self):
        import io
        from contextlib import redirect_stdout
        self.write("VERSION", "1.0.0\n")
        self.write("CHANGELOG.md", "# Changelog\n\n## 1.0.0\n\n- x\n")
        config = self.write("release.toml",
                            '[release]\nrepo = "A/B"\n[version]\n'
                            'source = "VERSION"\n[[asset]]\n'
                            'name = "x.bin"\npath = "nope.bin"\n')
        args = type("A", (), {"config": str(config), "version": None})()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = rk.cmd_plan(args)
        self.assertEqual(rc, 1)
        self.assertIn("Not release-ready", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
