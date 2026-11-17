#!/usr/bin/python3 -IB
"""Install a published release of this project. The panel runs it as root through pkexec.

    rog_update.py latest                     # newest release and the installed version, as JSON
    rog_update.py install v0.2.0             # download, verify and install (root)
    rog_update.py install v0.2.0 --dry-run   # everything except install.sh (no root needed)

Only the tag comes from the caller. The repository is fixed here. Only published releases with a
vX.Y.Z tag are accepted, never one older than or equal to the installed version, and the release's
VERSION file must match its tag. Python files are compiled (not run) as a sanity check before
service/install.sh from the release installs it.
"""
import argparse
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request

REPO = "gabiilie95/ASUS-ROG-Linux-Smart-Fan-Control"
API = "https://api.github.com/repos/" + REPO
TARBALL = "https://codeload.github.com/" + REPO + "/tar.gz/refs/tags/{tag}"
APP = Path("/usr/local/lib/rog-fand")
INSTALLED = APP / "VERSION"
WORK = Path("/var/lib/rog-fand/update")
AGENT = "rog-fand-updater"          # a generic client name; nothing about the user
MAX_JSON = 2 << 20
MAX_TARBALL = 64 << 20
TAG_RE = re.compile(r"v(\d{1,4})\.(\d{1,4})\.(\d{1,4})")
VERSION_RE = re.compile(r"(\d{1,4})\.(\d{1,4})\.(\d{1,4})(-dev)?")
REQUIRED = ("VERSION", "service/install.sh", "service/rog_fand.py", "service/rog_update.py", "ui/rog-panel-qt.py")


def version_key(text):
    """'0.2.0' -> (0, 2, 0, 1). A '-dev' build sorts before its release. None if not a version."""
    m = VERSION_RE.fullmatch((text or "").strip())
    return None if not m else (int(m[1]), int(m[2]), int(m[3]), 0 if m[4] else 1)


def tag_key(tag):
    m = TAG_RE.fullmatch(tag or "")
    return None if not m else (int(m[1]), int(m[2]), int(m[3]), 1)


def installed_version(path=None):
    try:
        return (path or INSTALLED).read_text().strip() or None
    except OSError:
        return None


def is_newer(tag, installed):
    """True when the release tag is newer than the installed version (unknown installs: never)."""
    key, current = tag_key(tag), version_key(installed)
    return key is not None and current is not None and key > current


def fetch(url, limit, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise ValueError("download larger than expected")
    return data


def newest_release(releases):
    """The highest published vX.Y.Z release from the GitHub API list (pre-releases included)."""
    best = None
    for r in releases if isinstance(releases, list) else ():
        if not isinstance(r, dict) or r.get("draft") or not isinstance(r.get("tag_name"), str):
            continue
        key = tag_key(r["tag_name"])
        if key and (best is None or key > best[0]):
            best = key, r
    return None if best is None else best[1]


def latest():
    return newest_release(json.loads(fetch(API + "/releases?per_page=30", MAX_JSON)))


def published(tag):
    try:
        r = json.loads(fetch(API + "/releases/tags/" + tag, MAX_JSON))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(f"{tag} is not a published release") from None
        raise
    if not isinstance(r, dict) or r.get("tag_name") != tag or r.get("draft"):
        raise ValueError(f"{tag} is not a published release")
    return r


def unpack(data, dest):
    """Extract with the 'data' filter (no absolute paths, links out of the tree, devices or
    set-id bits) and return the archive's single top folder."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")
    tops = list(dest.iterdir())
    if len(tops) != 1 or not tops[0].is_dir() or tops[0].is_symlink():
        raise ValueError("unexpected archive layout")
    return tops[0]


def check_tree(top, tag):
    for name in REQUIRED:
        path = top / name
        if path.is_symlink() or not path.is_file():
            if name == "VERSION":
                raise ValueError(f"{tag} has no VERSION file; it predates the updater")
            raise ValueError(f"{tag} is missing {name}")
    text = (top / "VERSION").read_text().strip()
    if text != tag[1:]:
        raise ValueError(f"{tag} VERSION file says {text!r}")
    for path in sorted(top.rglob("*.py")):
        if not path.is_symlink():
            compile(path.read_bytes(), str(path.relative_to(top)), "exec", dont_inherit=True)


def install(tag, dry_run=False, out=print):
    if tag_key(tag) is None:
        raise ValueError("tag must look like v1.2.3")
    if not dry_run and os.geteuid() != 0:
        raise PermissionError("run as root (the panel uses pkexec)")
    published(tag)
    current = installed_version()
    if not is_newer(tag, current):
        message = f"{tag} is not newer than the installed version ({current or 'unknown'})"
        if not dry_run:
            raise ValueError(message)
        out("dry run continues: " + message)
    out(f"downloading {tag}")
    data = fetch(TARBALL.format(tag=tag), MAX_TARBALL, timeout=60)
    if dry_run:
        work = Path(tempfile.mkdtemp(prefix="rog-update-"))
    else:
        shutil.rmtree(WORK, ignore_errors=True)
        WORK.mkdir(mode=0o700)
        work = WORK
    try:
        top = unpack(data, work)
        check_tree(top, tag)
        out(f"verified {tag}")
        if dry_run:
            out(f"dry run: would run service/install.sh install from {tag}")
            return
        out("installing")
        subprocess.run(["/bin/bash", str(top / "service" / "install.sh"), "install"], check=True, timeout=600,
                       stdin=subprocess.DEVNULL)
        out(f"installed {tag}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("latest", help="print the newest release and the installed version")
    inst = sub.add_parser("install", help="install a release (root)")
    inst.add_argument("tag")
    inst.add_argument("--dry-run", action="store_true", help="download and verify only")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "latest":
            r = latest()
            tag = r["tag_name"] if r else None
            print(json.dumps({"installed": installed_version(), "latest": tag,
                              "update": bool(tag and is_newer(tag, installed_version()))}))
        else:
            install(a.tag, a.dry_run, out=lambda line: print(line, flush=True))
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError, tarfile.TarError) as e:
        print(f"error: {e}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
