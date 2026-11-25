#!/usr/bin/env python3
"""Exercise the pipeline in a temporary checkout, without publishing or launching Cyclop."""
import os
from pathlib import Path
import plistlib
import shutil
import shlex
import subprocess
import tempfile
import unittest


PIPELINE = Path(__file__).with_name("pipeline.sh")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cyclop pipeline ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "Scripts").mkdir()
        if PIPELINE.exists():
            shutil.copy2(PIPELINE, self.root / "Scripts/pipeline.sh")
        (self.root / "Sources").mkdir()
        (self.root / "Sources/Example.swift").write_text('Text("Hello")\n')
        for language in ("en", "ru"):
            table = self.root / f"Resources/{language}.lproj/Localizable.strings"
            table.parent.mkdir(parents=True)
            table.write_text('"Hello" = "Hello";\n')
        (self.root / "docs/releases").mkdir(parents=True)
        (self.root / "docs/index.html").write_text('{"softwareVersion": "1.2.3"}')
        (self.root / "docs/releases/1.2.3.md").write_text("A useful release.\n")
        (self.root / "Scripts/version").write_text("VERSION=1.2.3\n")
        app = self.root / "build/Cyclop.app/Contents"
        (app / "MacOS").mkdir(parents=True)
        (app / "MacOS/Cyclop").write_text("#!/bin/sh\nexit 0\n")
        (app / "MacOS/Cyclop").chmod(0o755)
        (app / "Resources").mkdir()
        (app / "Resources/libcyclopmedia.dylib").touch()
        for language in ("en", "ru"):
            shutil.copytree(self.root / f"Resources/{language}.lproj", app / f"Resources/{language}.lproj")
        with (app / "Info.plist").open("wb") as stream:
            plistlib.dump({"CFBundleShortVersionString": "1.2.3"}, stream)
        for name in ("test", "bundle", "test-helper", "dmg", "release"):
            self.script(name, f'echo {name} >> "$PWD/calls"\n')
        binaries = self.root / "bin"
        binaries.mkdir()
        self.env = dict(os.environ, PATH=f"{binaries}:{os.environ['PATH']}")
        (binaries / "codesign").write_text("#!/bin/sh\nexit 0\n")
        (binaries / "codesign").chmod(0o755)

    def script(self, name, body):
        path = self.root / f"Scripts/{name}.sh"
        path.write_text("#!/bin/bash\nset -e\n" + body)
        path.chmod(0o755)

    def run_pipeline(self, *args):
        return subprocess.run(
            ["/bin/bash", str(self.root / "Scripts/pipeline.sh"), *args],
            cwd="/", env=self.env, text=True, capture_output=True,
        )

    def calls(self):
        path = self.root / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def prepare_release(self, push_url=None):
        # Use real git metadata; only fetch and GitHub calls are replaced, so
        # clean-tree, branch, tracking ref, and remote URL checks remain real.
        git = shutil.which("git")
        commands = [
            ["init", "--quiet", "-b", "main"],
            ["remote", "add", "origin", "https://github.com/example/cyclop.git"],
        ]
        if push_url:
            commands.append(["remote", "set-url", "--push", "origin", push_url])
        for args in commands:
            subprocess.run([git, *args], cwd=self.root, check=True, capture_output=True)
        (self.root / "bin/git").write_text(
            '#!/bin/bash\nif [ "$1" = fetch ]; then exit 0; fi\n'
            f'exec {shlex.quote(git)} "$@"\n'
        )
        (self.root / "bin/git").chmod(0o755)
        (self.root / "bin/gh").write_text('''#!/bin/bash
set -eu
case "$1 $2" in
  "auth status") exit 0 ;;
  "secret list") printf '%s\\n' DEVELOPER_ID_P12 DEVELOPER_ID_P12_PASSWORD NOTARY_KEY_ID NOTARY_ISSUER_ID NOTARY_KEY_P8 ;;
  "run list") case "$*" in *build.yml*) echo success ;; *) echo 123 ;; esac ;;
  "run watch") echo "$GH_REPO" > "$PWD/watched-repo" ;;
  "release view") echo "https://github.com/$GH_REPO/releases/tag/v1.2.3" ;;
  *) exit 99 ;;
esac
''')
        (self.root / "bin/gh").chmod(0o755)
        self.script("release", 'echo release >> "$PWD/calls"\necho "$GH_REPO" > "$PWD/published-repo"\n')
        subprocess.run([git, "add", "."], cwd=self.root, check=True, capture_output=True)
        subprocess.run([
            git, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
            "-c", "user.name=Pipeline Test", "-c", "user.email=pipeline@example.invalid",
            "commit", "--quiet", "-m", "Fixture",
        ], cwd=self.root, check=True, capture_output=True)
        subprocess.run([git, "update-ref", "refs/remotes/origin/main", "HEAD"],
                       cwd=self.root, check=True, capture_output=True)

    def test_default_builds_app_without_publishing(self):
        result = self.run_pipeline()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["test", "bundle", "test-helper"])

    def test_missing_translation_stops_before_build(self):
        (self.root / "Resources/ru.lproj/Localizable.strings").write_text("")
        result = self.run_pipeline()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Hello", result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

    def test_site_version_mismatch_stops_before_build(self):
        (self.root / "docs/index.html").write_text('{"softwareVersion": "0.0.1"}')
        result = self.run_pipeline()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("softwareVersion", result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

    def test_test_failure_stops_before_bundle(self):
        self.script("test", 'echo test >> "$PWD/calls"\nexit 42\n')
        result = self.run_pipeline()
        self.assertEqual(result.returncode, 42)
        self.assertEqual(self.calls(), ["test"])

    def test_incomplete_bundle_is_rejected(self):
        (self.root / "build/Cyclop.app/Contents/Resources/libcyclopmedia.dylib").unlink()
        result = self.run_pipeline()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), ["test", "bundle"])

    def test_dmg_builds_once_and_never_publishes(self):
        result = self.run_pipeline("dmg")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["test", "dmg", "test-helper"])

    def test_release_requires_manual_checks_before_any_side_effect(self):
        result = self.run_pipeline("release")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--manual-checked", result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

    def test_bad_arguments_do_not_build(self):
        for args in (("wat",), ("check", "--manual-checked"), ("check", "extra")):
            with self.subTest(args=args):
                self.assertNotEqual(self.run_pipeline(*args).returncode, 0)
                self.assertEqual(self.calls(), [])

    def test_release_uses_origin_for_publication_and_monitoring(self):
        self.prepare_release()
        self.env["GH_REPO"] = "upstream/other"
        result = self.run_pipeline("release", "--manual-checked")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), ["test", "bundle", "test-helper", "release"])
        self.assertEqual((self.root / "published-repo").read_text().strip(), "example/cyclop")
        self.assertEqual((self.root / "watched-repo").read_text().strip(), "example/cyclop")

    def test_release_rejects_different_push_repository(self):
        self.prepare_release("git@github.com:upstream/other.git")
        result = self.run_pipeline("release", "--manual-checked")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

    def test_release_accepts_ssh_push_to_same_repository(self):
        self.prepare_release("git@github.com:example/cyclop.git")
        result = self.run_pipeline("release", "--manual-checked")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_helper_failure_prevents_publication(self):
        self.script("test-helper", 'echo test-helper >> "$PWD/calls"\nexit 43\n')
        self.prepare_release()
        result = self.run_pipeline("release", "--manual-checked")
        self.assertEqual(result.returncode, 43)
        self.assertEqual(self.calls(), ["test", "bundle", "test-helper"])


if __name__ == "__main__":
    unittest.main()
