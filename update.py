#!/usr/bin/env python3
"""
Self-update.

Two situations, two mechanisms:

  Built .exe   Downloads the newest exe from the repository's GitHub Releases
               and swaps itself for it. Windows won't let a running program be
               overwritten, but it will let one be *renamed* -- so the live exe
               is moved aside and the new one takes its name. The old copy is
               deleted on the next start, which doubles as the rollback if the
               new one won't run.

  From source  Downloads the tracked .py files at the newest commit, the same
               way it always has.

Worth understanding before using either: an update replaces the code this app
runs. Whoever can push to that repository decides what runs on your machine the
next time you start it. That is fine for your own repo; don't point it at
someone else's.

Nothing happens silently. Checking is a button, installing is a second button,
and a confirmation sits between them.
"""

import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request

import paths

HERE = paths.home()
CACHE = paths.DATA_DIR
BACKUP = os.path.join(CACHE, "backup")
STAMP = os.path.join(CACHE, "installed.json")
CONFIG = os.path.join(HERE, "config.json")

# The files a source install is allowed to replace.
TRACKED = ["app.py", "engine.py", "store.py", "paths.py", "update.py",
           "make_icon.py", "run.bat", "README.md"]

UA = {"User-Agent": "ffxiv-arb-updater/1.0", "Accept": "application/vnd.github+json"}
TIMEOUT = 30
MAX_DOWNLOAD = 200 * 1024 * 1024

# GitHub serves release assets from its own host and then redirects to a CDN.
# Both are expected; anything else is not.
ASSET_HOSTS = ("https://github.com/", "https://api.github.com/",
               "https://objects.githubusercontent.com/",
               "https://release-assets.githubusercontent.com/")


class UpdateError(Exception):
    """Anything that stops an update, phrased for a person to read."""


# ----------------------------------------------------------------------------
# Which repository
# ----------------------------------------------------------------------------

def repo_slug():
    """'owner/name' for this install, or None if it isn't configured."""
    for path in (CONFIG, paths.resource("config.json")):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                slug = (json.load(fh) or {}).get("repo")
            if slug and re.fullmatch(r"[\w.-]+/[\w.-]+", slug):
                return slug
        except (OSError, json.JSONDecodeError, AttributeError):
            continue

    if getattr(paths, "REPO", "") and re.fullmatch(r"[\w.-]+/[\w.-]+", paths.REPO):
        return paths.REPO

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


def _get(url, hosts=("https://api.github.com/",), binary=False):
    if not url.startswith(hosts):
        raise UpdateError(f"Refusing to fetch an unexpected URL: {url[:70]}")
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if not r.geturl().startswith(hosts):
                raise UpdateError("The download redirected somewhere unexpected; "
                                  "stopping rather than running it.")
            body = r.read(MAX_DOWNLOAD + 1)
            if len(body) > MAX_DOWNLOAD:
                raise UpdateError("That download is far larger than expected.")
            return body if binary else json.loads(body.decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError(
                "GitHub returned 'not found'. Either the repository is private "
                "(the updater can't read those without a token) or it has no "
                "releases yet.") from e
        if e.code == 403:
            raise UpdateError("GitHub rate-limited this check. Try again shortly.") from e
        raise UpdateError(f"GitHub returned HTTP {e.code}.") from e
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"Couldn't reach GitHub: {e}") from e
    except json.JSONDecodeError as e:
        raise UpdateError("GitHub sent a response this tool couldn't read.") from e


# ----------------------------------------------------------------------------
# Versions
# ----------------------------------------------------------------------------

def parse_version(text):
    """'v1.7.1' -> (1, 7, 1). Unparseable versions sort lowest."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", str(text or ""))
    return tuple(int(g) for g in m.groups()) if m else (0, 0, 0)


def installed_sha():
    try:
        with open(STAMP, encoding="utf-8-sig") as fh:
            return (json.load(fh) or {}).get("sha")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def remember(sha, slug):
    os.makedirs(CACHE, exist_ok=True)
    try:
        with open(STAMP, "w", encoding="utf-8") as fh:
            json.dump({"sha": sha, "slug": slug}, fh)
    except OSError:
        pass


# ----------------------------------------------------------------------------
# Checking
# ----------------------------------------------------------------------------

def check():
    """What's the newest version, and is it newer than this one?"""
    slug = repo_slug()
    if not slug:
        raise UpdateError(
            "No repository configured yet. Put {\"repo\": \"you/ffxiv-arb\"} in "
            "config.json next to the app, or run it from a git clone.")

    if paths.FROZEN:
        rel = _get(f"https://api.github.com/repos/{slug}/releases/latest")
        tag = rel.get("tag_name") or ""
        asset = next((a for a in (rel.get("assets") or [])
                      if str(a.get("name", "")).lower().endswith(".exe")), None)
        if not asset:
            raise UpdateError(
                f"The newest release of {slug} ({tag or 'untagged'}) has no .exe "
                f"attached, so there's nothing to install.")
        newest = parse_version(tag)
        mine = parse_version(paths.VERSION)
        return {
            "mode": "exe", "slug": slug, "version": tag or "?",
            "short": tag or "?",
            "message": (rel.get("name") or "").strip(),
            "date": (rel.get("published_at") or "")[:10],
            "url": asset.get("browser_download_url"),
            "size": asset.get("size") or 0,
            "available": newest > mine,
            "known": True,
        }

    data = _get(f"https://api.github.com/repos/{slug}/commits?per_page=1")
    if not isinstance(data, list) or not data:
        raise UpdateError(f"{slug} has no commits yet.")
    head = data[0]
    sha = head.get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise UpdateError("GitHub returned a commit id this tool didn't recognise.")
    current = installed_sha()
    message = ((head.get("commit") or {}).get("message") or "").splitlines()[:1]
    return {
        "mode": "source", "slug": slug, "sha": sha, "short": sha[:7],
        "version": sha[:7],
        "message": message[0] if message else "",
        "date": ((head.get("commit") or {}).get("author") or {}).get("date", "")[:10],
        "available": current is not None and current != sha,
        "known": current is not None,
    }


# ----------------------------------------------------------------------------
# Installing
# ----------------------------------------------------------------------------

def _download_file(slug, sha, name):
    url = f"https://raw.githubusercontent.com/{slug}/{sha}/{name}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if not r.geturl().startswith(f"https://raw.githubusercontent.com/{slug}/"):
                raise UpdateError(f"Download of {name} redirected off-site.")
            return r.read(MAX_DOWNLOAD)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise UpdateError(f"Couldn't download {name}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"Couldn't download {name}: {e}") from e


def install(info):
    """Apply an update. Returns a short description of what changed."""
    if info.get("mode") == "exe":
        return _install_exe(info)
    return _install_source(info)


def _install_exe(info):
    """Swap the running exe for the newly downloaded one.

    Windows refuses to overwrite a running executable but allows renaming it,
    so the live file is moved aside and the download takes its place. If
    anything fails partway the original name is restored, so a failed update
    leaves a working app rather than none.
    """
    url = info.get("url") or ""
    blob = _get(url, hosts=ASSET_HOSTS, binary=True)
    if len(blob) < 1_000_000 or blob[:2] != b"MZ":
        raise UpdateError("That download isn't a Windows program. Nothing was "
                          "changed.")

    live = os.path.abspath(sys.executable)
    folder = os.path.dirname(live)
    incoming = os.path.join(folder, "update-incoming.exe")
    retired = os.path.join(folder, "previous-version.exe")

    try:
        with open(incoming, "wb") as fh:
            fh.write(blob)
    except OSError as e:
        raise UpdateError(f"Couldn't write the new version: {e}") from e

    try:
        if os.path.exists(retired):
            os.remove(retired)
    except OSError:
        pass

    try:
        os.replace(live, retired)      # allowed even while running
    except OSError as e:
        _quiet_remove(incoming)
        raise UpdateError(
            f"Couldn't move the current version aside: {e}. If the app is in a "
            f"folder you can't write to, move it somewhere like your Desktop "
            f"and try again.") from e

    try:
        os.replace(incoming, live)
    except OSError as e:
        os.replace(retired, live)      # put things back exactly as they were
        raise UpdateError(f"Couldn't put the new version in place: {e}") from e

    return [f"{os.path.basename(live)} -> {info.get('version', 'newest')}"]


def _install_source(info):
    slug, sha = info["slug"], info["sha"]
    staged = {}
    for name in TRACKED:
        body = _download_file(slug, sha, name)
        if body is None:
            continue
        if name.endswith(".py"):
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

    remember(sha, slug)
    return sorted(staged)


def _quiet_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def tidy_after_restart():
    """Delete the version we replaced. Called once at startup.

    Kept until now on purpose: while it exists you can rename it back by hand
    if a new build turns out to be broken.
    """
    if not paths.FROZEN:
        return
    folder = os.path.dirname(os.path.abspath(sys.executable))
    for leftover in ("previous-version.exe", "update-incoming.exe"):
        _quiet_remove(os.path.join(folder, leftover))


def restore():
    """Put the previous version back after a bad source update."""
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
        print(f"Repository : {info['slug']}  ({info['mode']} install)")
        print(f"Installed  : {paths.VERSION}")
        print(f"Newest     : {info['version']}  {info.get('date','')}  "
              f"{info.get('message','')}")
        if info["available"]:
            print("\nInstalling…")
            print("Replaced:", ", ".join(install(info)))
            print("Restart the app to run it.")
        else:
            print("\nUp to date.")
    except UpdateError as exc:
        raise SystemExit(f"Update check failed: {exc}")
