#!/usr/bin/env python3
"""Tests for rog_update.py: no network, no root and no real installation."""
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import rog_update as up


def tarball(files, top="ASUS-ROG-Linux-Smart-Fan-Control-0.2.0", extra=()):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        for name, text in files.items():
            body = text.encode()
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(body)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(body))
        for info in extra:
            tar.addfile(info)
    return data.getvalue()


def release_files(version="0.2.0"):
    return {"VERSION": version + "\n", "service/install.sh": "echo install\n", "service/rog_fand.py": "x = 1\n",
            "service/rog_update.py": "y = 2\n", "ui/rog-panel-qt.py": "z = 3\n"}


class VersionTest(unittest.TestCase):
    def test_order(self):
        self.assertLess(up.version_key("0.2.0-dev"), up.tag_key("v0.2.0"))
        self.assertLess(up.tag_key("v0.1.9"), up.version_key("0.2.0-dev"))
        self.assertLess(up.tag_key("v0.9.0"), up.tag_key("v0.10.0"))

    def test_is_newer(self):
        self.assertTrue(up.is_newer("v0.2.0", "0.2.0-dev"))
        self.assertTrue(up.is_newer("v0.2.1", "0.2.0"))
        self.assertFalse(up.is_newer("v0.2.0", "0.2.0"))
        self.assertFalse(up.is_newer("v0.1.0", "0.2.0-dev"))
        self.assertFalse(up.is_newer("v9.0.0", None), "an unknown install is never offered updates")
        for bad in ("0.2.0", "v0.2", "v0.2.0-rc1", "v0.2.0\n", "v1.2.3/../x", ""):
            self.assertIsNone(up.tag_key(bad), bad)

    def test_newest_release(self):
        releases = [dict(tag_name="v0.3.0", draft=True), dict(tag_name="nightly"), "junk",
                    dict(tag_name="v0.2.0", prerelease=True), dict(tag_name="v0.1.0")]
        self.assertEqual(up.newest_release(releases)["tag_name"], "v0.2.0")
        self.assertIsNone(up.newest_release({"message": "rate limited"}))

    def test_installed_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "VERSION"
            self.assertIsNone(up.installed_version(path))
            path.write_text("0.2.0-dev\n")
            self.assertEqual(up.installed_version(path), "0.2.0-dev")


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dest = Path(tmp.name)

    def test_good_tree(self):
        top = up.unpack(tarball(release_files()), self.dest)
        up.check_tree(top, "v0.2.0")

    def test_path_traversal_rejected(self):
        evil = tarfile.TarInfo("../evil")
        with self.assertRaises(tarfile.TarError):
            up.unpack(tarball(release_files(), extra=[evil]), self.dest)
        self.assertFalse((self.dest.parent / "evil").exists())

    def test_link_out_of_tree_rejected(self):
        link = tarfile.TarInfo("ASUS-ROG-Linux-Smart-Fan-Control-0.2.0/passwd")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        with self.assertRaises(tarfile.TarError):
            up.unpack(tarball(release_files(), extra=[link]), self.dest)

    def test_version_must_match_tag(self):
        top = up.unpack(tarball(release_files("0.2.0-dev")), self.dest)
        with self.assertRaisesRegex(ValueError, "VERSION file says"):
            up.check_tree(top, "v0.2.0")

    def test_old_release_without_version(self):
        files = release_files()
        del files["VERSION"]
        top = up.unpack(tarball(files), self.dest)
        with self.assertRaisesRegex(ValueError, "predates the updater"):
            up.check_tree(top, "v0.2.0")

    def test_syntax_error_rejected(self):
        files = release_files()
        files["ui/broken.py"] = "def (\n"
        top = up.unpack(tarball(files), self.dest)
        with self.assertRaises(SyntaxError):
            up.check_tree(top, "v0.2.0")


class InstallTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.installed = self.tmp / "VERSION"
        self.installed.write_text("0.2.0-dev\n")
        self.work = self.tmp / "state" / "update"
        self.work.parent.mkdir()
        self.urls = []
        self.files = release_files()
        for name, value in (("INSTALLED", self.installed), ("WORK", self.work)):
            p = patch.object(up, name, value)
            p.start()
            self.addCleanup(p.stop)

    def fake_fetch(self, url, limit, timeout=20):
        self.urls.append(url)
        if url == up.API + "/releases/tags/v0.2.0":
            return json.dumps(dict(tag_name="v0.2.0", draft=False)).encode()
        if url == up.TARBALL.format(tag="v0.2.0"):
            return tarball(self.files)
        raise up.urllib.error.HTTPError(url, 404, "Not Found", None, None)

    def run_install(self, tag="v0.2.0", dry_run=False, root=True):
        calls = []
        def fake_run(argv, **kw):
            calls.append((argv, Path(argv[1]).is_file()))
            return subprocess.CompletedProcess(argv, 0)
        with patch.object(up, "fetch", self.fake_fetch), patch.object(up.os, "geteuid", return_value=0 if root else 1000), \
             patch.object(up.subprocess, "run", side_effect=fake_run):
            up.install(tag, dry_run, out=lambda line: None)
        return calls

    def test_installs_newer_release(self):
        calls = self.run_install()
        self.assertEqual(len(calls), 1)
        argv, existed = calls[0]
        self.assertEqual(argv[0], "/bin/bash")
        self.assertTrue(argv[1].startswith(str(self.work)) and argv[1].endswith("service/install.sh") and existed)
        self.assertEqual(argv[2:], ["install"])
        self.assertFalse(self.work.exists(), "the download is removed after installing")

    def test_refuses_not_newer(self):
        self.installed.write_text("0.2.0\n")
        with self.assertRaisesRegex(ValueError, "not newer"):
            self.run_install()
        self.assertNotIn(up.TARBALL.format(tag="v0.2.0"), self.urls, "nothing downloaded")

    def test_refuses_unpublished(self):
        with self.assertRaisesRegex(ValueError, "not a published release"):
            self.run_install("v0.3.0")

    def test_needs_root(self):
        with self.assertRaises(PermissionError):
            self.run_install(root=False)

    def test_bad_tag(self):
        with self.assertRaisesRegex(ValueError, "v1.2.3"):
            self.run_install("v0.2.0; rm -rf /")

    def test_dry_run_installs_nothing(self):
        self.installed.write_text("0.2.0\n")    # not newer: a dry run still verifies
        self.assertEqual(self.run_install(dry_run=True, root=False), [])
        self.assertFalse(self.work.exists())

    def test_mismatched_release_not_installed(self):
        self.files["VERSION"] = "0.1.0\n"
        with self.assertRaisesRegex(ValueError, "VERSION file says"):
            self.run_install()
        self.assertFalse(self.work.exists())

    def test_cli_reports_errors(self):
        with patch.object(up, "fetch", side_effect=OSError("offline")), \
             patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(up.main(["latest"]), 1)
        self.assertIn("offline", err.getvalue())


if __name__ == "__main__":
    unittest.main()
