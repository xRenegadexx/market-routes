#!/usr/bin/env python3
"""
Cut a release: bump the version, build the exe, commit, push, publish.

    python release.py 1.8.2 "Fixed the thing"
    python release.py 1.8.2 "Fixed the thing" -n "- Fixed X" -n "- Added Y"

That is the whole job. Testers press Check for updates and get it.

Release notes are read on a phone-sized card next to a download button, so
write bullets, one per change, and let the commit carry any reasoning. Repeat
-n for each bullet rather than putting line breaks inside one quoted string:
the whole command then stays on a single line you can paste in one go.

Each step is checked before the next one runs, and nothing is pushed until the
build has actually succeeded -- so a failed build leaves the repo untouched
rather than half-released.
"""

import argparse
import io
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "FFXIV Market Routes"

GH_CANDIDATES = [
    shutil.which("gh"),
    r"C:\Program Files\GitHub CLI\gh.exe",
    r"C:\Program Files (x86)\GitHub CLI\gh.exe",
]


def gh_path():
    for candidate in GH_CANDIDATES:
        if candidate and os.path.exists(candidate):
            return candidate
    sys.exit("GitHub CLI not found. Install it with:  winget install GitHub.cli")


def run(cmd, **kw):
    """Run a command, stop the release if it fails."""
    result = subprocess.run(cmd, cwd=HERE, text=True, capture_output=True, **kw)
    if result.returncode != 0:
        sys.exit(f"\nFailed: {' '.join(str(c) for c in cmd)}\n"
                 f"{(result.stderr or result.stdout).strip()[:600]}")
    return (result.stdout or "").strip()


def step(n, total, text):
    print(f"[{n}/{total}] {text}", flush=True)


def set_version(version):
    path = os.path.join(HERE, "paths.py")
    source = io.open(path, encoding="utf-8").read()
    current = re.search(r'VERSION = "([^"]+)"', source)
    if not current:
        sys.exit("Couldn't find VERSION in paths.py.")
    updated = re.sub(r'VERSION = "[^"]+"', f'VERSION = "{version}"', source, count=1)
    io.open(path, "w", encoding="utf-8").write(updated)
    return current.group(1)


def main():
    ap = argparse.ArgumentParser(description="Build and publish a release.")
    ap.add_argument("version", help="e.g. 1.8.2 -- three numbers, no 'v'")
    ap.add_argument("message", help="what changed, in a few words")
    ap.add_argument("-n", "--notes", action="append", default=None,
                    metavar="BULLET",
                    help="one bullet of the release notes; repeat for each "
                         "change, e.g. -n \"- Fixed X\" -n \"- Added Y\". "
                         "Keep them to what changed; nobody reads a paragraph "
                         "on a release page")
    args = ap.parse_args()

    version = args.version.lstrip("vV")

    # Each -n is one line. A literal \n typed inside a bullet is almost
    # certainly meant as a line break rather than those two characters, so
    # honour it -- it costs nothing and it is an easy thing to reach for.
    notes = "\n".join(args.notes) if args.notes else args.message
    notes = notes.replace("\\n", "\n")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        sys.exit(f"'{args.version}' needs to be three numbers, like 1.8.2. "
                 f"The app compares versions numerically, and a tag it can't "
                 f"read counts as 0.0.0 -- nobody would ever be offered it.")

    gh = gh_path()
    total = 6

    step(1, total, "Checking the working tree and GitHub login")
    auth = subprocess.run([gh, "auth", "status"], cwd=HERE, text=True,
                          capture_output=True)
    if auth.returncode != 0:
        sys.exit("\n".join([
            "GitHub CLI isn't logged in *from this terminal*.",
            "",
            "If you have already run 'gh auth login', the usual cause is an",
            "elevated prompt: a Run-as-administrator window gets a different",
            "view of the Windows credential store, so it cannot see the token",
            "a normal window saved. Close this window, open a plain Command",
            "Prompt or PowerShell (without 'run as administrator'), and retry.",
            "",
            "If it still refuses, log in from this terminal:",
            '    "' + gh + '" auth login',
            "  -> GitHub.com -> HTTPS -> Yes -> Login with a web browser",
        ]))

    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if branch != "main":
        sys.exit(f"You're on branch '{branch}', not main.")
    # Ask GitHub, not the local tag list. A release published from another
    # machine -- or by an earlier run of this script -- leaves no local tag, so
    # checking locally lets the whole build and push go through before failing
    # at the last step, which is exactly the wrong place to find out.
    clash = subprocess.run([gh, "release", "view", f"v{version}"],
                           cwd=HERE, text=True, capture_output=True)
    if clash.returncode == 0:
        sys.exit(f"Release v{version} already exists on GitHub. Pick a higher "
                 f"number.")
    if run(["git", "tag", "-l", f"v{version}"]):
        sys.exit(f"Tag v{version} already exists locally. Pick a higher number.")

    dirty = run(["git", "status", "--porcelain"])
    if not dirty:
        print("      nothing has changed since the last release; "
              "the version bump alone will be the change")

    step(2, total, f"Setting the version to {version}")
    previous = set_version(version)
    print(f"      {previous} -> {version}")

    step(3, total, "Building the exe (this takes a minute or two)")
    dist = os.path.join(HERE, "builds", version)
    run([sys.executable, "-m", "PyInstaller", "--onefile", "--windowed",
         "--clean", "--noconfirm", "--name", APP_NAME,
         "--icon", os.path.join(HERE, "icon.ico"),
         "--add-data", f"{os.path.join(HERE, 'icon.ico')};.",
         "--distpath", dist, "--workpath", os.path.join(HERE, "build"),
         "--specpath", os.path.join(HERE, "build"),
         os.path.join(HERE, "app.py")])
    exe = os.path.join(dist, f"{APP_NAME}.exe")
    if not os.path.exists(exe):
        sys.exit("The build finished but produced no exe.")
    print(f"      {os.path.getsize(exe) / 1048576:.1f} MB")

    step(4, total, "Committing and pushing")
    run(["git", "add", "-A"])
    run(["git", "commit", "-m", args.message])
    run(["git", "push"])

    step(5, total, f"Publishing release v{version}")
    run([gh, "release", "create", f"v{version}", exe,
         "--title", f"v{version}",
         "--notes", notes])

    step(6, total, "Verifying testers will see it")
    latest = run([gh, "release", "view", "--json", "tagName,assets",
                  "--jq", '"\\(.tagName) with \\(.assets | length) file(s)"'])
    print(f"      newest release is now {latest}")

    print(f"\nDone. Anyone running an older build will be offered v{version} "
          f"next time they press Check for updates.")


if __name__ == "__main__":
    main()
