#!/usr/bin/env python3
"""
Self-update for the FFXIV market tool.

The app pulls its own source files from one GitHub repository over HTTPS and
swaps them in. That repository is worked out from the folder's git remote, or
from config.json if there's no git checkout -- it is never taken from anything
downloaded, so a compromised response can't redirect the updater somewhere else.

Worth understanding before you use it: an update replaces the code this app runs
next time it starts. Whoever can push to that repository can change what runs on
your machine. That's fine for your own repo; don't point it at someone else's.

Nothing updates silently. Checking is a button, installing is a second button,
and the previous version is kept in .cache/backup so you can put it back.
"""

import json
import os
import re
import shutil
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".cache")
BACKUP = os.path.join(CACHE, "backup")
STAMP = os.path.join(CACHE, "installed.json")
CONFIG = os.path.join(HERE, "config.json")

# The files an update is allowed to replace. Anything else in the repo is ignored.
TRACKED = ["app.py", "engine.py", "update.py", "run.bat", "README.md"]

UA = {"User-Agent": "ffxiv-arb-updater/1.0", "Accept": "application/vnd.github+json"}
TIMEOUT = 20


class UpdateError(Exception):
    """Anything that stops an update, phrased for a person to read."""


# ----------------------------------------------------------------------------
# Where are we updating from
# ----------------------------------------------------------------------------

def repo_slug():
    """'owner/name' for this install, or None if it isn't configured yet."""
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as fh:
                slug = (json.load(fh) or {}).get("repo")
            if slug and re.fullmatch(r"[\w.-]+/[\w.-]+", slug):
                return slug
        except (OSError, json.JSONDecodeError, AttributeError):
            pass

    # Fall back to the git remote, so a normal clone needs no config at all.
    git_config = os.path.join(HERE, ".git", "config")
    if os.path.exists(git_config):
        try:
            with open(git_config, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return None
        m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?\s", text)
        if m:
            return m.group(1)
    return None


def _api(url):
    if not url.startswith("https://api.github.com/"):
        raise UpdateError("Refusing to call a non-GitHub URL.")
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if not r.geturl().startswith("https://api.github.com/"):
                raise UpdateError("GitHub redirected the request off-site.")
            return json.loads(r.read(4 * 1024 * 1024).decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError(
                "GitHub returned 'not found'. If the repository is private, the "
                "updater can't read it without a token — make it public, or "
                "update by running: git pull") from e
        if e.code == 403:
            raise UpdateError(
                "GitHub rate-limited this check. Try again in a few minutes.") from e
        raise UpdateError(f"GitHub returned HTTP {e.code}.") from e
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"Couldn't reach GitHub: {e}") from e
    except json.JSONDecodeError as e:
        raise UpdateError("GitHub sent a response this tool couldn't read.") from e


def installed_sha():
    try:
        with open(STAMP, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("sha")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def check():
    """Ask GitHub what the newest commit is.

    Returns a dict describing the situation; never raises for 'no update'.
    """
    slug = repo_slug()
    if not slug:
        raise UpdateError(
            "No repository configured yet. Push this folder to GitHub, then "
            "either clone it back or put {\"repo\": \"you/ffxiv-arb\"} in "
            "config.json next to app.py.")
    data = _api(f"https://api.github.com/repos/{slug}/commits?per_page=1")
    if not isinstance(data, list) or not data:
        raise UpdateError(f"{slug} has no commits yet.")
    head = data[0]
    sha = head.get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise UpdateError("GitHub returned a commit id this tool didn't recognise.")
    message = ((head.get("commit") or {}).get("message") or "").splitlines()[:1]
    current = installed_sha()
    return {
        "slug": slug,
        "sha": sha,
        "short": sha[:7],
        "message": message[0] if message else "",
        "date": ((head.get("commit") or {}).get("author") or {}).get("date", "")[:10],
        # First run after a manual clone has no stamp: treat that as up to date
        # and record it, rather than nagging about an update to what's already here.
        "available": current is not None and current != sha,
        "known": current is not None,
    }


# ----------------------------------------------------------------------------
# Installing
# ----------------------------------------------------------------------------

def _download(slug, sha, name):
    url = f"https://raw.githubusercontent.com/{slug}/{sha}/{name}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if not r.geturl().startswith(f"https://raw.githubusercontent.com/{slug}/"):
                raise UpdateError(f"Download of {name} redirected off-site.")
            return r.read(8 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None          # file simply isn't in the repo; skip it
        raise UpdateError(f"Couldn't download {name}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"Couldn't download {name}: {e}") from e


def install(info):
    """Fetch the tracked files at that commit and swap them in.

    Everything is downloaded and checked before a single live file is touched,
    so a download that dies halfway can't leave a half-updated app behind.
    """
    slug, sha = info["slug"], info["sha"]
    staged = {}
    for name in TRACKED:
        body = _download(slug, sha, name)
        if body is None:
            continue
        if name.endswith(".py"):
            # Refuse anything that isn't valid Python rather than break the app.
            try:
                compile(body.decode("utf-8"), name, "exec")
            except (SyntaxError, UnicodeDecodeError) as e:
                raise UpdateError(
                    f"The downloaded {name} isn't valid Python ({e}). "
                    f"Nothing was changed.") from e
        staged[name] = body
    if not staged:
        raise UpdateError("That commit contains none of this app's files.")

    os.makedirs(BACKUP, exist_ok=True)
    for name in staged:
        live = os.path.join(HERE, name)
        if os.path.exists(live):
            shutil.copy2(live, os.path.join(BACKUP, name))
    for name, body in staged.items():
        tmp = os.path.join(HERE, name + ".new")
        with open(tmp, "wb") as fh:
            fh.write(body)
        os.replace(tmp, os.path.join(HERE, name))

    os.makedirs(CACHE, exist_ok=True)
    with open(STAMP, "w", encoding="utf-8") as fh:
        json.dump({"sha": sha, "slug": slug, "files": sorted(staged)}, fh)
    return sorted(staged)


def remember(sha, slug):
    """Record the current commit without changing any files."""
    os.makedirs(CACHE, exist_ok=True)
    with open(STAMP, "w", encoding="utf-8") as fh:
        json.dump({"sha": sha, "slug": slug}, fh)


def restore():
    """Put the previous version back after a bad update."""
    if not os.path.isdir(BACKUP):
        raise UpdateError("There's no backup to restore.")
    names = [n for n in os.listdir(BACKUP) if n in TRACKED]
    if not names:
        raise UpdateError("There's no backup to restore.")
    for name in names:
        shutil.copy2(os.path.join(BACKUP, name), os.path.join(HERE, name))
    return sorted(names)


if __name__ == "__main__":
    try:
        info = check()
        print(f"Repository : {info['slug']}")
        print(f"Latest     : {info['short']}  {info['date']}  {info['message']}")
        print(f"Installed  : {installed_sha() or '(not recorded yet)'}")
        if info["available"]:
            print("\nAn update is available. Installing…")
            print("Replaced:", ", ".join(install(info)))
            print("Restart the app to run it.")
        else:
            remember(info["sha"], info["slug"])
            print("\nUp to date.")
    except UpdateError as exc:
        raise SystemExit(f"Update check failed: {exc}")
