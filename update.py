#!/usr/bin/env python3
"""
Self-update.

Two situations, two mechanisms:

  Built .exe   Downloads the newest exe from the repository's GitHub Releases
               and swaps itself for it. Windows won't let a running program be
               overwritten, but it will let one be *renamed* -- so the live exe
               is moved aside and the new one takes its name. The moved-aside
               copy goes into the data folder rather than staying next to the
               app, and is deleted as soon as the app closes.

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
import subprocess
import sys
import tempfile
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
           "make_icon.py", "README.md"]

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


# The exe we replaced, waiting to be deleted. It can't be deleted while it is
# still the image of this running process; see _delete_when_free.
_PENDING_DELETE = None
RETIRED_NAME = "previous-version.exe"


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
    """Apply an update.

    Returns (changed, pending). `pending` means the swap happens when the app
    closes, so the caller must say so rather than claiming it is done.
    """
    if info.get("mode") == "exe":
        return _install_exe(info)
    return _install_source(info), False


def _install_exe(info):
    """Swap the running exe for the newly downloaded one.

    Two mechanisms, because the neat one is not always allowed. Windows will
    usually let a running program be renamed, and when it does the swap is
    instant. But antivirus and Controlled Folder Access both block that rename,
    and Downloads is a common place for it to be blocked -- seen in practice as
    "Access is denied" after the download had already succeeded.

    So when the rename is refused we fall back to leaving a small script that
    waits for this process to exit, puts the new file in place and relaunches.
    Slower, but it works wherever the folder is writable at all.

    Returns (changed, pending) -- pending is True when a restart is required to
    finish the job.
    """
    url = info.get("url") or ""
    blob = _get(url, hosts=ASSET_HOSTS, binary=True)
    if len(blob) < 1_000_000 or blob[:2] != b"MZ":
        raise UpdateError("That download isn't a Windows program. Nothing was "
                          "changed.")

    live = os.path.abspath(sys.executable)
    folder = os.path.dirname(live)
    incoming = os.path.join(folder, "update-incoming.exe")
    retired = _retire_target(folder)

    try:
        with open(incoming, "wb") as fh:
            fh.write(blob)
    except OSError as e:
        raise UpdateError(
            f"Couldn't write the new version next to the app: {e}\n\n"
            f"Move the app somewhere you can write to -- your Desktop is "
            f"fine -- and try again.") from e

    _sweep_retired(folder)

    # The fast path: rename the running image aside and take its name. Windows
    # allows a running program to be renamed but not deleted or overwritten,
    # and a rename can cross folders as long as it stays on the same volume --
    # which is what lets the old copy leave the app's folder entirely.
    try:
        os.replace(live, retired)
    except OSError:
        return _finish_on_exit(live, incoming), True

    try:
        os.replace(incoming, live)
    except OSError as e:
        os.replace(retired, live)      # put things back exactly as they were
        _quiet_remove(incoming)
        raise UpdateError(f"Couldn't put the new version in place: {e}") from e

    # It is out of the way now but still on disk, because it is this process's
    # own image. Nothing can delete it until we exit, so queue it up.
    globals()["_PENDING_DELETE"] = retired
    return [f"{os.path.basename(live)} -> {info.get('version', 'newest')}"], False


def _retire_target(folder):
    """Where to park the exe we are replacing.

    Not next to the app if it can be helped. Two programs sitting side by side
    reads as "the old version is still installed", and the point of updating in
    place was to avoid exactly that. The data folder is ours and nobody browses
    it, so it goes there.

    The catch is that renaming a running program only works within one volume;
    across drives Windows has to copy, and the file is locked against that. The
    data folder is normally beside the exe, but it falls back to LocalAppData
    when the app's folder isn't writable -- possibly a different drive. So when
    the volumes differ, keep the old copy here and simply hide it.
    """
    def drive(path):
        return os.path.splitdrive(os.path.abspath(path))[0].lower()

    if drive(folder) == drive(CACHE):
        try:
            parked = os.path.join(CACHE, "retired")
            os.makedirs(parked, exist_ok=True)
            return os.path.join(parked, RETIRED_NAME)
        except OSError:
            pass
    return os.path.join(folder, RETIRED_NAME)


def _sweep_retired(folder):
    """Clear out any previous-version.exe from either of its two homes."""
    _quiet_remove(os.path.join(folder, RETIRED_NAME))
    _quiet_remove(os.path.join(CACHE, "retired", RETIRED_NAME))


def finish_cleanup():
    """Arrange for the replaced exe to be gone once this process has.

    Called on the way out. Until now the old copy has been unkillable -- it is
    the image this very process is running from -- which is why it used to
    survive until the next launch. A small script started here outlives us by a
    few seconds and does what we can't.

    Safe to call when no update happened; it does nothing.
    """
    target = _PENDING_DELETE
    if not target or not os.path.exists(target):
        return
    globals()["_PENDING_DELETE"] = None
    _delete_when_free(target)


def _delete_when_free(target):
    """Leave a script that keeps trying the delete until it works.

    No need to check whether we have exited: the delete simply fails while the
    file is still a running image and succeeds the moment it isn't. Trying is
    the wait.

    It lives in the temp folder rather than beside the app -- swapping one bit
    of visible litter for another would miss the point -- and removes itself
    when it's done.
    """
    if sys.platform != "win32":
        _quiet_remove(target)
        return
    body = f"""@echo off
rem Written by {paths.APP_NAME} to delete the version it replaced.
setlocal
set tries=0
:retry
del /f /q "{target}" >nul 2>&1
if not exist "{target}" goto done
set /a tries+=1
if %tries% GEQ 40 goto done
ping -n 2 127.0.0.1 >nul
goto retry
:done
del "%~f0"
"""
    try:
        script = os.path.join(tempfile.gettempdir(), "market-routes-tidy.cmd")
        with open(script, "w", encoding="ascii", errors="replace") as fh:
            fh.write(body)
        subprocess.Popen(["cmd", "/c", script],
                         cwd=tempfile.gettempdir(),
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         close_fds=True)
    except (OSError, ValueError):
        # Not worth telling anyone about on the way out the door. The startup
        # sweep will catch it next time instead.
        pass


def _finish_on_exit(live, incoming):
    """Leave a script that completes the swap once this process has gone.

    It simply retries the move until it succeeds, which it will the moment the
    exe is no longer running, then relaunches and deletes itself.
    """
    folder = os.path.dirname(live)
    script = os.path.join(folder, "finish-update.cmd")
    body = f"""@echo off
rem Written by the app to finish an update it could not apply while running.
setlocal
set tries=0
:retry
set /a tries+=1
move /Y "{incoming}" "{live}" >nul 2>&1
if not errorlevel 1 goto done
if %tries% GEQ 60 goto giveup
ping -n 2 127.0.0.1 >nul
goto retry
:done
start "" "{live}"
goto cleanup
:giveup
rem Could not replace it in two minutes; leave the download for the user.
:cleanup
del "%~f0"
"""
    try:
        with open(script, "w", encoding="ascii", errors="replace") as fh:
            fh.write(body)
    except OSError as e:
        _quiet_remove(incoming)
        raise UpdateError(
            f"This folder won't allow the app to replace itself ({e}).\n\n"
            f"Move it somewhere like your Desktop and try again.") from e

    try:
        subprocess.Popen(["cmd", "/c", script], cwd=folder,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         close_fds=True)
    except OSError as e:
        _quiet_remove(script)
        _quiet_remove(incoming)
        raise UpdateError(f"Couldn't start the installer step: {e}") from e

    return [os.path.basename(live)]


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
    """Clear anything an update left behind. Called once at startup.

    The delete normally happens as the old version closes. This is the backstop
    for when it couldn't -- a crash instead of a clean exit, or antivirus
    stopping the script from running -- so the folder can't accumulate old
    versions either way. Every previous build is still on the Releases page if
    one is ever needed back.
    """
    if not paths.FROZEN:
        return
    folder = os.path.dirname(os.path.abspath(sys.executable))
    for leftover in (RETIRED_NAME, "update-incoming.exe", "finish-update.cmd"):
        _quiet_remove(os.path.join(folder, leftover))
    _quiet_remove(os.path.join(CACHE, "retired", RETIRED_NAME))
    try:
        os.rmdir(os.path.join(CACHE, "retired"))
    except OSError:
        pass


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
