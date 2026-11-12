"""
Update check and self-update from the plugin's GitHub releases.

The plugin is usually installed from a URL (not the Decky store), so Decky does not update it.
The release asset is the same zip users install by hand (<name>/ + plugin files). Installing
replaces the plugin's files, dist/index.js and main.py last: Decky's live reload then restarts
the plugin with the new code. The replacement runs in a separate process (a copy of this module
in /tmp): Decky stops the plugin as soon as index.js or main.py changes, and an update done by
the plugin itself could be cut off halfway.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

REPO = "mrFunkill/decky-dualsense-wired"
API = "https://api.github.com/repos/%s/releases?per_page=10" % REPO
MAX_ZIP = 50 * 1024 * 1024
CHECK_INTERVAL = 6 * 3600


def _version_tuple(v):
    parts = []
    for p in v.lstrip("vV").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts + [0] * (3 - len(parts)))


def _fetch(url, accept="application/vnd.github+json", limit=MAX_ZIP):
    """HTTPS GET. Decky's bundled Python may lack CA certificates: fall back to the system curl."""
    try:
        req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "decky-dualsense-wired"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read(limit + 1)
    except Exception:  # noqa: BLE001 - SSL/certificate problems in the bundled Python
        env = {k: v for k, v in os.environ.items() if k not in ("LD_LIBRARY_PATH", "LD_PRELOAD")}
        data = subprocess.run(["/usr/bin/curl", "-fsSL", "--max-time", "60", "-H", "Accept: " + accept,
                               "-H", "User-Agent: decky-dualsense-wired", url],
                              check=True, capture_output=True, env=env).stdout
    if len(data) > limit:
        raise ValueError("download too large")
    return data


class Updater:
    def __init__(self, name, version, plugin_dir, logger):
        self.name = name
        self.version = version
        self.plugin_dir = plugin_dir
        self.log = logger
        self._checked_at = 0.0
        self._result = None

    def check(self, force=False):
        """{"current", "latest", "available", "url" (release page), "asset" (zip URL)} or an error."""
        if self._result and not force and time.time() - self._checked_at < CHECK_INTERVAL:
            return self._result
        result = {"current": self.version, "latest": None, "available": False}
        try:
            releases = json.loads(_fetch(API))
            release = next(r for r in releases if not r.get("draft") and not r.get("prerelease"))
            asset = next((a for a in release.get("assets", [])
                          if a["name"].startswith(self.name) and a["name"].endswith(".zip")), None)
            result["latest"] = release["tag_name"].lstrip("v")
            result["url"] = release.get("html_url")
            result["asset"] = asset and asset["browser_download_url"]
            result["available"] = bool(asset) and _version_tuple(result["latest"]) > _version_tuple(self.version)
        except Exception as e:  # noqa: BLE001 - offline, rate limit, ...
            result["error"] = str(e)
            self.log.info("Update check failed: %s", e)
        self._result = result
        self._checked_at = time.time()
        return result

    def install(self):
        """Download the latest release zip and replace the plugin's files with it."""
        info = self.check(force=True)
        if not info.get("available"):
            raise RuntimeError("no update available")
        data = _fetch(info["asset"], accept="application/octet-stream")
        stage = tempfile.mkdtemp(prefix=self.name + "-update-", dir="/tmp")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for member in z.namelist():
                    target = os.path.realpath(os.path.join(stage, member))
                    if not target.startswith(os.path.realpath(stage) + os.sep):
                        raise ValueError("unsafe path in the zip: " + member)
                z.extractall(stage)
            root = os.path.join(stage, self.name)
            with open(os.path.join(root, "plugin.json")) as f:
                json.load(f)
            with open(os.path.join(root, "package.json")) as f:
                new_version = json.load(f).get("version")
            if new_version != info["latest"]:
                raise ValueError("zip contains version %s, expected %s" % (new_version, info["latest"]))
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        self._apply_detached(stage, root)
        self.log.info("Updated to %s; Decky reloads the plugin", new_version)
        return new_version

    def _apply_detached(self, stage, root):
        """Replace the files from a separate process; wait for it unless Decky stops us first."""
        script = "/tmp/%s-update.py" % self.name
        shutil.copyfile(os.path.abspath(__file__), script)
        log_path = "/tmp/%s-update.log" % self.name
        # Decky's bundled libraries (LD_LIBRARY_PATH) must not leak into the system python.
        env = {k: v for k, v in os.environ.items() if k not in ("LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME")}
        with open(log_path, "w") as log:
            proc = subprocess.Popen(["/usr/bin/python3", "-I", script, stage, root, self.plugin_dir],
                                    stdout=log, stderr=log, env=env, stdin=subprocess.DEVNULL,
                                    start_new_session=True, close_fds=True)
        if proc.wait(timeout=60) != 0:
            with open(log_path) as f:
                raise RuntimeError("installing the files failed: " + f.read().strip()[-300:])


# Reloads are triggered by these files: they go last, main.py at the very end.
RELOAD_FILES = ("dist/index.js", "main.py")


def apply_update(root, plugin_dir):
    """Make plugin_dir contain exactly the files of root (an extracted release)."""
    new_files = set()
    for dirpath, _, files in os.walk(root):
        for f in files:
            new_files.add(os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/"))
    # Drop files that are no longer part of the plugin.
    for dirpath, _, files in os.walk(plugin_dir):
        for f in files:
            rel = os.path.relpath(os.path.join(dirpath, f), plugin_dir).replace(os.sep, "/")
            if rel not in new_files and "__pycache__" not in rel:
                os.remove(os.path.join(dirpath, f))
    order = sorted(new_files, key=lambda r: RELOAD_FILES.index(r) + 1 if r in RELOAD_FILES else 0)
    for rel in order:
        src = os.path.join(root, rel)
        dst = os.path.join(plugin_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".new"
        shutil.copyfile(src, tmp)
        # Zips made on Windows carry no permission bits: the helper in bin/ must stay executable.
        os.chmod(tmp, 0o755 if rel.startswith("bin/") else 0o644)
        os.replace(tmp, dst)


if __name__ == "__main__":
    # /usr/bin/python3 -I <copy of this file> <stage dir> <extracted plugin root> <plugin dir>
    stage_dir, new_root, target = sys.argv[1:4]
    try:
        apply_update(new_root, target)
        print("update installed into", target)
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
        if os.path.abspath(__file__).startswith("/tmp/"):
            os.remove(os.path.abspath(__file__))
