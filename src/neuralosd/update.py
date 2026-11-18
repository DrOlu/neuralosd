"""Self-update for an installed neuralOSd.

    neuralosd update --check          # current -> latest, no change
    neuralosd update --yes            # perform the update
    neuralosd update --json --check   # for schedulers

There is **no silent background updater**: the runtime is deterministic and
offline-first, so a network check on import or on every ask would violate the
product's promises. ``update`` is explicit, and a scheduler can run
``update --check``.

Two install shapes are handled:

* **pip / uv / pipx / uv tool** — the running interpreter upgrades the
  package with its own package manager. Editable checkouts are refused
  (upgrading a wheel over an editable tree is not an update).
* **standalone binary** (Nuitka; also the npm launcher's download) — the
  matching asset for this platform is fetched from the GitHub release and
  verified against the release's ``sha256`` digest before an atomic swap.
  A backup of the previous binary is kept beside it. On Windows a running
  executable cannot be replaced, so the verified file is written as
  ``<exe>.new`` and the exact next step is printed.

Every failure is refused, never half-applied: download to a temporary file
first, verify, then ``os.replace``. No update is ever run with elevated
privileges, and no network call happens unless the command is invoked.
"""
import hashlib
import importlib.metadata
import json
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import __version__

PYPI_JSON = "https://pypi.org/pypi/neuralosd/json"
RELEASES_API = "https://api.github.com/repos/DrOlu/neuralosd/releases"
USER_AGENT = f"neuralosd-update/{__version__}"
MAX_DOWNLOAD = 700 * 1024 * 1024          # release assets are <= ~313 MB
LOCK_STALE_SECONDS = 3600
_CHANNELS = ("stable", "pre")

_VERSION_RE = re.compile(
    r"^\s*v?(\d+)\.(\d+)\.(\d+)"
    r"(?:(a|b|rc)(\d+))?"
    r"(?:\+[0-9A-Za-z.\-]+)?\s*$")
_PRE_RANK = {"a": 0, "b": 1, "rc": 2}


class UpdateError(Exception):
    """A refused or failed update; the message is safe to show the user."""


# ── versions ─────────────────────────────────────────────────────────────

def parse_version(text):
    """Return a sortable tuple, or None when the string is not a version.

    ``3.4.0`` > ``3.4.0rc1`` > ``3.4.0b2`` > ``3.4.0a1``; ``3.10`` beats
    ``3.9`` (numeric, not lexical). Build metadata is ignored.
    """
    m = _VERSION_RE.match(text or "")
    if not m:
        return None
    major, minor, patch = (int(x) for x in m.group(1, 2, 3))
    pre, num = m.group(4), m.group(5)
    if pre:
        return (major, minor, patch, _PRE_RANK[pre], int(num))
    return (major, minor, patch, 3, 0)


def is_newer(candidate, current):
    a, b = parse_version(candidate), parse_version(current)
    if a is None or b is None:
        return False
    return a > b


def is_prerelease(text):
    m = _VERSION_RE.match(text or "")
    return bool(m and m.group(4))


# ── install shape ────────────────────────────────────────────────────────

def install_kind():
    """``frozen``, ``pip`` or ``unknown`` — how this copy was installed."""
    if (getattr(sys, "frozen", False) or "__compiled__" in globals()
            or "__nuitka_version__" in globals()):
        return "frozen"
    try:
        importlib.metadata.distribution("neuralosd")
        return "pip"
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def install_via():
    """Which package manager owns a pip-shaped install, when detectable."""
    prefix = (getattr(sys, "prefix", "") or "").replace("\\", "/")
    if "/pipx/venvs/" in prefix:
        return "pipx"
    if "/uv/tools/" in prefix:
        return "uv-tool"
    return "pip"


def is_editable():
    if install_kind() == "frozen":
        return False
    try:
        from pathlib import Path
        dist = importlib.metadata.distribution("neuralosd")
        # setuptools develop / old-style editable installs have no wheel.
        meta = str(getattr(dist, "_path", "") or "")
        if meta.endswith((".egg-info", ".egg-link")):
            return True
        entry = next((f for f in (dist.files or [])
                      if f.name == "direct_url.json"), None)
        if entry is None:
            return False
        raw = Path(dist.locate_file(entry)).read_text(encoding="utf-8")
    except Exception:
        return False
    try:
        return bool(json.loads(raw or "{}").get("dir_info", {}).get("editable"))
    except ValueError:
        return False


def update_command(latest):
    """The command that upgrades this install, or None when we cannot tell.

    It is shown to the user by ``--check`` and executed by ``update``. The
    running interpreter is used, never a guessed ``pip`` on PATH.
    """
    via = install_via()
    if via == "pipx" and shutil.which("pipx"):
        return ["pipx", "upgrade", "neuralosd"]
    if via == "uv-tool" and shutil.which("uv"):
        return ["uv", "tool", "upgrade", "neuralosd"]
    if _pip_available():
        return [sys.executable, "-m", "pip", "install", "--upgrade",
                f"neuralosd=={latest}"]
    if shutil.which("uv"):
        return ["uv", "pip", "install", "--python", sys.executable,
                f"neuralosd=={latest}"]
    return None


def _pip_available():
    try:
        p = subprocess.run([sys.executable, "-m", "pip", "--version"],
                           capture_output=True, timeout=30)
        return p.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ── network ──────────────────────────────────────────────────────────────

def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise UpdateError(f"HTTP {resp.status} from {url}")
        return resp.read()


def _get_json(url, timeout=15):
    try:
        return json.loads(_get(url, timeout))
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"HTTP {exc.code} from {url}")
    except urllib.error.URLError as exc:
        raise UpdateError(f"cannot reach {url}: {exc.reason}")
    except (TimeoutError, OSError) as exc:
        raise UpdateError(f"cannot reach {url}: {exc}")
    except ValueError:
        raise UpdateError(f"invalid JSON from {url}")


def latest_pypi(fetch=_get_json, timeout=15, channel="stable"):
    data = fetch(PYPI_JSON, timeout)
    version = (data.get("info") or {}).get("version")
    if channel == "pre":
        candidates = [v for v in (data.get("releases") or {})
                      if parse_version(v)]
        if candidates:
            return max(candidates, key=parse_version)
    if not version or parse_version(version) is None:
        raise UpdateError("PyPI returned no usable version")
    return version


def latest_release(fetch=_get_json, timeout=15, channel="stable"):
    """The newest GitHub release tag (``v`` prefix removed) or None."""
    if channel == "pre":
        releases = fetch(RELEASES_API + "?per_page=20", timeout)
    else:
        releases = [fetch(RELEASES_API + "/latest", timeout)]
    if isinstance(releases, dict):
        releases = [releases]
    for rel in releases:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        if channel != "pre" and rel.get("prerelease"):
            continue
        tag = (rel.get("tag_name") or "").lstrip("v")
        if parse_version(tag):
            return tag
    return None


def latest_version(kind, channel="stable", fetch=_get_json, timeout=15,
                   release_fetch=None):
    """Latest published version for this install shape and channel."""
    if channel not in _CHANNELS:
        raise UpdateError(f"unknown channel: {channel}")
    if kind == "frozen":
        return latest_release(fetch=release_fetch or _get_json,
                              timeout=timeout, channel=channel)
    return latest_pypi(fetch=fetch, timeout=timeout, channel=channel)


# ── standalone binary update ─────────────────────────────────────────────

_OS = {"darwin": "macos", "linux": "linux", "win32": "windows"}
_ARCH = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64",
         "amd64": "x64"}


def asset_name(platform=None, machine=None, variant="base"):
    platform = (platform or sys.platform).lower()
    if machine is None:
        try:
            machine = _platform.machine()
        except Exception:
            machine = ""
    machine = machine.lower()
    osname = _OS.get(platform)
    arch = _ARCH.get(machine)
    if not osname or not arch:
        raise UpdateError(f"no release asset for {platform}-{machine}")
    stem = "neuralosd" if variant == "base" else f"neuralosd-{variant}"
    ext = ".exe" if osname == "windows" else ""
    return f"{stem}-{osname}-{arch}{ext}"


def variant_from_path(path):
    name = os.path.basename(path or "").lower()
    if "boxlite" in name:
        return "boxlite"
    if "msb" in name:
        return "msb"
    return "base"


def _release_for(version, fetch=_get_json, timeout=15):
    tag = "v" + str(version).lstrip("v")
    return fetch(f"{RELEASES_API}/tags/{tag}", timeout)


def _find_asset(release, name):
    for asset in release.get("assets") or []:
        if asset.get("name") == name:
            return asset
    return None


def _digest(asset):
    raw = asset.get("digest") or ""
    if raw.startswith("sha256:"):
        value = raw.split(":", 1)[1].strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", value):
            return value
    return None


def download_verified(url, dest, digest=None, opener=urllib.request.urlopen,
                      timeout=120):
    """Stream ``url`` to ``dest`` and verify its sha256 before returning."""
    if not digest:
        raise UpdateError("release asset has no sha256 digest; refusing "
                          "an unverified binary (use pip or npm instead)")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    total = 0
    try:
        with opener(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                raise UpdateError(
                    f"HTTP {getattr(resp, 'status', '?')} downloading asset")
            with open(dest, "wb") as fh:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD:
                        raise UpdateError("release asset is too large")
                    h.update(chunk)
                    fh.write(chunk)
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"HTTP {exc.code} downloading {url}")
    except urllib.error.URLError as exc:
        raise UpdateError(f"cannot download {url}: {exc.reason}")
    except (TimeoutError, OSError) as exc:
        raise UpdateError(f"cannot download {url}: {exc}")
    if h.hexdigest() != digest:
        raise UpdateError("sha256 mismatch: the downloaded binary does not "
                          "match the release digest; nothing was replaced")
    return total


def _lock_path(dest):
    return dest + ".update.lock"


def _take_lock(dest):
    lock = _lock_path(dest)
    for attempt in (0, 1):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return lock
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lock)
            except OSError:
                age = LOCK_STALE_SECONDS + 1
            if attempt == 0 and age > LOCK_STALE_SECONDS:
                try:
                    os.unlink(lock)
                except OSError:
                    pass
                continue
            raise UpdateError("another update is already running "
                              f"({lock})")
    raise UpdateError("cannot take the update lock")


def _release_lock(lock):
    try:
        os.unlink(lock)
    except OSError:
        pass


def swap_verified(exe, staged):
    """Atomically replace ``exe`` with the verified ``staged`` file."""
    backup = exe + ".bak"
    try:
        shutil.copy2(exe, backup)
    except OSError:
        backup = None
    os.chmod(staged, 0o755)
    try:
        os.replace(staged, exe)
    except OSError as exc:
        raise UpdateError(f"cannot replace {exe}: {exc}")
    return backup


def update_binary(version, fetch=_get_json, opener=urllib.request.urlopen,
                  timeout=120, exe=None):
    """Update the running standalone executable. Returns a result dict."""
    exe = os.path.realpath(exe or sys.executable)
    directory = os.path.dirname(exe) or "."
    if not os.access(directory, os.W_OK) or not os.access(exe, os.W_OK):
        raise UpdateError(f"{directory} is not writable; install neuralosd "
                          "somewhere the user owns, or update it with the "
                          "package manager that installed it")
    name = asset_name(variant=variant_from_path(exe))
    release = _release_for(f"v{version}", fetch=fetch)
    asset = _find_asset(release, name)
    if asset is None:
        raise UpdateError(f"release v{version} has no asset {name}")
    digest = _digest(asset)
    url = asset.get("browser_download_url")
    if not url:
        raise UpdateError(f"release asset {name} has no download URL")
    lock = _take_lock(exe)
    staged = exe + ".new"
    keep_staged = False
    try:
        size = download_verified(url, staged, digest, opener=opener,
                                 timeout=timeout)
        if os.name == "nt":
            keep_staged = True
            return {"action": "deferred", "path": staged,
                    "size": size,
                    "message": f"verified update written to {staged}; close "
                               "neuralosd, then rename it over the installed "
                               "executable"}
        backup = swap_verified(exe, staged)
        return {"action": "updated", "path": exe, "size": size,
                "backup": backup}
    finally:
        _release_lock(lock)
        if not keep_staged and os.path.exists(staged):
            try:
                os.unlink(staged)
            except OSError:
                pass


def update_pip(version, command=None, run=subprocess.run, timeout=600):
    if is_editable():
        raise UpdateError(
            "this is an editable development checkout; updating would "
            "replace the source tree. Use `git pull` (or reinstall with "
            "`pip install -e .`) instead.")
    cmd = command or update_command(version)
    if cmd is None:
        raise UpdateError(
            "no package manager found for this install; run "
            f"`{sys.executable} -m pip install --upgrade neuralosd=={version}` "
            "or use the installer you originally used")
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"update command failed to start: {exc}")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
        raise UpdateError("update command failed"
                          + (": " + " | ".join(tail) if tail else ""))
    return {"action": "updated", "command": cmd,
            "output": (proc.stdout or "").strip()[-2000:]}


# ── the command the CLI calls ────────────────────────────────────────────

def check(channel="stable", timeout=15, fetch=_get_json,
          release_fetch=None, kind=None):
    current = __version__
    kind = kind or install_kind()
    latest = latest_version(kind, channel=channel, fetch=fetch,
                            timeout=timeout, release_fetch=release_fetch)
    if latest is None:
        raise UpdateError("no published release found")
    available = is_newer(latest, current)
    plan = None
    if kind == "frozen":
        plan = f"download {asset_name(variant=variant_from_path(sys.executable))}"
    elif kind == "pip":
        cmd = update_command(latest)
        plan = " ".join(cmd) if cmd else None
    return {"current": current, "latest": latest, "install": kind,
            "channel": channel, "update_available": available,
            "plan": plan}


def run(check_only=False, yes=False, force=False, channel="stable",
        timeout=15, fetch=_get_json, release_fetch=None, opener=None,
        kind=None, confirm=None):
    """Check (and optionally apply) an update. Returns a result dict.

    ``confirm`` is used by tests and by the CLI's TTY prompt: it receives
    the result dict and returns True to go ahead.
    """
    info = check(channel=channel, timeout=timeout, fetch=fetch,
                 release_fetch=release_fetch, kind=kind)
    info["requested"] = bool(info["update_available"] or force)
    if check_only or not info["requested"]:
        info["action"] = "none"
        return info
    if confirm is not None and not confirm(info):
        info["action"] = "declined"
        return info
    kind = info["install"]
    if kind == "frozen":
        outcome = update_binary(info["latest"], fetch=release_fetch or fetch,
                                opener=opener or urllib.request.urlopen,
                                timeout=max(timeout, 120))
    elif kind == "pip":
        outcome = update_pip(info["latest"])
    else:
        raise UpdateError("cannot determine how neuralosd was installed; "
                          "use pip, npm or the standalone binary")
    info.update(outcome)
    if outcome.get("action") == "updated" and kind == "pip":
        info["restart_required"] = True
    return info
