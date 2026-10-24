#!/usr/bin/env python3
"""lg-hue-sync-deployer

Baut adeze/lg-hue-sync fuer LG webOS (armv7-unknown-linux-gnueabi, glibc <= 2.28),
erkennt neue Upstream-Commits und deployt das Binary per Web-Button auf den TV.
Nur Python-Standardbibliothek (Debian Buster liefert Python 3.7).
"""
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def env_str(name, default):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


CONFIG_DIR = env_str("CONFIG_DIR", "/config")
REPO_URL = env_str("UPSTREAM_REPO", "https://github.com/adeze/lg-hue-sync.git")
BRANCH = env_str("UPSTREAM_BRANCH", "main")
TV_IP = env_str("TV_IP", "")
TV_SSH_PORT = env_str("TV_SSH_PORT", "22")
TV_DASH_PORT = env_str("TV_DASHBOARD_PORT", "8088")
WEBUI_PORT = int(env_str("WEBUI_PORT", "8789"))
CHECK_INTERVAL_HOURS = float(env_str("CHECK_INTERVAL_HOURS", "6"))
AUTO_BUILD = env_bool("AUTO_BUILD")
AUTO_DEPLOY = env_bool("AUTO_DEPLOY")
UI_TOKEN = env_str("UI_TOKEN", "")
SSH_KEY_TYPE = env_str("SSH_KEY_TYPE", "ed25519")

TARGET = "armv7-unknown-linux-gnueabi"
GLIBC_CEILING = (2, 28)
REMOTE_DIR = "/var/home/root/lg-hue-sync"
SRC = os.path.join(CONFIG_DIR, "src")
STAGE = os.path.join(CONFIG_DIR, "stage")
TARGET_CACHE = os.path.join(CONFIG_DIR, "target-cache")
CARGO_CACHE = os.path.join(CONFIG_DIR, "cargo")
ARTIFACT_DIR = os.path.join(CONFIG_DIR, "artifact")
ARTIFACT = os.path.join(ARTIFACT_DIR, "lg-hue-sync")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
LOG_FILE = os.path.join(CONFIG_DIR, "job.log")
SSH_DIR = os.path.join(CONFIG_DIR, ".ssh")
SSH_KEY = os.path.join(SSH_DIR, "id_" + SSH_KEY_TYPE)
APP_DIR = os.path.dirname(os.path.abspath(__file__))

_state_lock = threading.Lock()
_job_lock = threading.Lock()
_current = {"name": None, "started": None}


# --------------------------------------------------------------------------- state

def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def load_state():
    try:
        with open(STATE_FILE) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def update_state(**changes):
    with _state_lock:
        state = load_state()
        state.update(changes)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as handle:
            json.dump(state, handle, indent=2)
        os.replace(tmp, STATE_FILE)
        return state


# --------------------------------------------------------------------------- logging / processes

def log(message):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n"
    with open(LOG_FILE, "a") as handle:
        handle.write(line)


def rotate_log():
    try:
        if os.path.getsize(LOG_FILE) > 2 * 1024 * 1024:
            with open(LOG_FILE, "rb") as handle:
                handle.seek(-256 * 1024, os.SEEK_END)
                tail = handle.read()
            with open(LOG_FILE, "wb") as handle:
                handle.write(b"[... log gekuerzt ...]\n" + tail)
    except OSError:
        pass


def log_tail(limit=64 * 1024):
    try:
        with open(LOG_FILE, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def run(cmd, cwd=None, env=None):
    log("$ " + " ".join(cmd))
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    with open(LOG_FILE, "a") as handle:
        proc = subprocess.Popen(cmd, cwd=cwd, env=full_env, stdout=handle,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        code = proc.wait()
    log("-> exit %d" % code)
    return code


def capture(cmd, cwd=None, timeout=60, env=None):
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    try:
        result = subprocess.run(cmd, cwd=cwd, env=full_env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as exc:
        return 127, "", str(exc)


# --------------------------------------------------------------------------- ssh

def ensure_ssh():
    os.makedirs(SSH_DIR, exist_ok=True)
    os.chmod(SSH_DIR, 0o700)
    if not os.path.isfile(SSH_KEY):
        cmd = ["ssh-keygen", "-q", "-t", SSH_KEY_TYPE, "-N", "", "-C", "lg-hue-sync-deployer", "-f", SSH_KEY]
        if SSH_KEY_TYPE == "rsa":
            cmd[4:4] = ["-b", "3072"]
        code, _, err = capture(cmd)
        if code != 0:
            log("ssh-keygen fehlgeschlagen: " + err.strip())
    # BatchMode: deploy.sh / make deploy-bin duerfen nie auf eine Passwortabfrage warten.
    with open(os.path.join(SSH_DIR, "config"), "w") as handle:
        handle.write("Host *\n"
                     "  IdentityFile %s\n"
                     "  BatchMode yes\n"
                     "  StrictHostKeyChecking accept-new\n"
                     "  ConnectTimeout 5\n"
                     "  ServerAliveInterval 15\n" % SSH_KEY)
    os.chmod(os.path.join(SSH_DIR, "config"), 0o600)


def public_key():
    try:
        with open(SSH_KEY + ".pub") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def ssh_cmd(remote):
    return ["ssh", "-p", TV_SSH_PORT, "root@" + TV_IP, remote]


def tv_ssh(remote, timeout=20):
    return capture(ssh_cmd(remote), timeout=timeout)


def require_tv():
    if not TV_IP:
        log("TV_IP ist nicht gesetzt (Container-Variable).")
        return False
    return True


def tv_reachable():
    code, out, err = tv_ssh("echo ok", timeout=15)
    if code == 0 and out.strip() == "ok":
        return True
    log("SSH zum TV fehlgeschlagen (rc=%d): %s" % (code, (err or out).strip()))
    return False


# --------------------------------------------------------------------------- source / build

def git_head():
    code, out, _ = capture(["git", "-C", SRC, "rev-parse", "HEAD"])
    return out.strip() if code == 0 else ""


def cargo_version():
    try:
        with open(os.path.join(SRC, "Cargo.toml")) as handle:
            text = handle.read()
    except OSError:
        return ""
    package = re.search(r"\[package\](.*?)(\n\[|\Z)", text, re.S)
    if not package:
        return ""
    match = re.search(r'^\s*version\s*=\s*"([^"]+)"', package.group(1), re.M)
    return match.group(1) if match else ""


def ensure_src():
    if not os.path.isdir(os.path.join(SRC, ".git")):
        if os.path.exists(SRC):
            shutil.rmtree(SRC)
        return run(["git", "clone", "--branch", BRANCH, REPO_URL, SRC]) == 0
    if run(["git", "-C", SRC, "fetch", "origin", BRANCH]) != 0:
        return False
    if run(["git", "-C", SRC, "reset", "--hard", "origin/" + BRANCH]) != 0:
        return False
    run(["git", "-C", SRC, "clean", "-fd"])
    return True


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_elf(path):
    """Gleiche Kriterien wie upstream: ELF32 LE, ARM, EABI5, soft-float, glibc <= 2.28."""
    with open(path, "rb") as handle:
        header = handle.read(52)
    if len(header) < 52 or header[:4] != b"\x7fELF":
        return False, "kein ELF"
    if header[4] != 1 or header[5] != 1:
        return False, "nicht ELF32 little-endian"
    machine = int.from_bytes(header[18:20], "little")
    if machine != 40:
        return False, "e_machine=%d (erwartet 40/ARM)" % machine
    flags = int.from_bytes(header[36:40], "little")
    if (flags >> 24) & 0xFF != 5:
        return False, "nicht EABI5 (e_flags=0x%x)" % flags
    if flags & 0x400:
        return False, "hard-float ABI, erwartet soft-float"
    code, out, err = capture(["arm-linux-gnueabi-readelf", "-V", path])
    if code != 0:
        return False, "readelf fehlgeschlagen: " + err.strip()
    versions = [(int(a), int(b)) for a, b in re.findall(r"GLIBC_(\d+)\.(\d+)", out)]
    highest = max(versions) if versions else (0, 0)
    if highest > GLIBC_CEILING:
        return False, "benoetigt GLIBC_%d.%d > 2.28" % highest
    return True, "ELF ok: ARM EABI5 soft-float, max GLIBC_%d.%d" % highest


def job_check():
    code, out, err = capture(["git", "ls-remote", REPO_URL, "refs/heads/" + BRANCH], timeout=60)
    if code != 0 or not out.strip():
        log("Upstream nicht erreichbar: " + err.strip())
        return False
    remote = out.split()[0]
    pending = []
    if os.path.isdir(os.path.join(SRC, ".git")):
        if run(["git", "-C", SRC, "fetch", "origin", BRANCH]) == 0:
            code, out, _ = capture(["git", "-C", SRC, "log", "--format=%h %ad %s", "--date=short",
                                    "HEAD..origin/" + BRANCH, "-n", "50"])
            if code == 0:
                pending = [line for line in out.splitlines() if line.strip()]
    state = update_state(remote_commit=remote, remote_checked_at=now_iso(), pending=pending)
    built = state.get("built_commit") or ""
    if built == remote:
        log("Gebaute Version ist aktuell (%s)." % remote[:10])
    else:
        log("Upstream %s, gebaut %s -> Update verfuegbar." % (remote[:10], built[:10] or "-"))
    return True


def job_build():
    if not ensure_src():
        return False
    commit = git_head()
    version = cargo_version()
    run(["rustc", "--version"])
    log("Baue lg-hue-sync %s (%s) fuer %s ..." % (version or "?", commit[:10], TARGET))
    env = {"CARGO_HOME": CARGO_CACHE, "CARGO_TARGET_DIR": TARGET_CACHE}
    if run(["cargo", "build", "--locked", "--target", TARGET, "--release"], cwd=SRC, env=env) != 0:
        log("Build fehlgeschlagen.")
        return False
    built = os.path.join(TARGET_CACHE, TARGET, "release", "lg-hue-sync")
    ok, message = verify_elf(built)
    log(message)
    if not ok:
        return False
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    if os.path.isfile(ARTIFACT):
        shutil.copy2(ARTIFACT, ARTIFACT + ".previous")
    shutil.copy2(built, ARTIFACT + ".tmp")
    os.replace(ARTIFACT + ".tmp", ARTIFACT)
    os.chmod(ARTIFACT, 0o755)
    digest = sha256_file(ARTIFACT)
    update_state(built_commit=commit, built_version=version, built_sha256=digest,
                 built_at=now_iso(), remote_commit=commit, pending=[])
    log("Artefakt: %s sha256=%s" % (ARTIFACT, digest))
    return True


# --------------------------------------------------------------------------- deploy

def prepare_stage():
    """Sauberes Arbeitsverzeichnis fuer die Upstream-Skripte.

    Kein webos-app/: deploy.sh ueberspringt dann das Launcher-IPK (braucht uv + ares-rs).
    """
    if not os.path.isfile(ARTIFACT):
        log("Kein gebautes Binary vorhanden. Erst 'Holen & bauen'.")
        return False
    if not os.path.isdir(os.path.join(SRC, "scripts")):
        log("Quellcode fehlt (scripts/). Erst 'Holen & bauen'.")
        return False
    if os.path.exists(STAGE):
        shutil.rmtree(STAGE)
    os.makedirs(STAGE)
    shutil.copytree(os.path.join(SRC, "scripts"), os.path.join(STAGE, "scripts"))
    shutil.copy2(os.path.join(SRC, "Makefile"), os.path.join(STAGE, "Makefile"))
    release = os.path.join(STAGE, "target", TARGET, "release")
    os.makedirs(release)
    shutil.copy2(ARTIFACT, os.path.join(release, "lg-hue-sync"))
    return True


def tv_installed():
    code, _, _ = tv_ssh("test -x %s/lg-hue-sync && test -f /var/lib/webosbrew/init.d/50-lg-hue-sync" % REMOTE_DIR)
    return code == 0


def job_tv_status():
    if not require_tv():
        return False
    reachable = tv_reachable()
    tv_sha = ""
    service = ""
    if reachable:
        code, out, _ = tv_ssh("sha256sum %s/lg-hue-sync 2>/dev/null" % REMOTE_DIR)
        tv_sha = out.split()[0] if code == 0 and out.strip() else ""
        _, out, _ = tv_ssh("systemctl is-active lg-hue-sync 2>/dev/null")
        service = out.strip() or "nicht installiert"
    api = None
    try:
        url = "http://%s:%s/api/status" % (TV_IP, TV_DASH_PORT)
        with urllib.request.urlopen(url, timeout=4) as resp:
            api = json.loads(resp.read(64 * 1024).decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 - Status ist rein informativ
        api = {"error": str(exc)}
    update_state(tv_reachable=reachable, tv_sha256=tv_sha, tv_service=service,
                 tv_api=api, tv_checked_at=now_iso())
    log("TV: ssh=%s service=%s sha=%s" % ("ok" if reachable else "nein", service or "-", tv_sha[:16] or "-"))
    return reachable


def job_deploy_first():
    if not require_tv() or not prepare_stage() or not tv_reachable():
        return False
    log("Erstinstallation ueber upstream scripts/deploy.sh --reuse (ohne Launcher-Kachel).")
    ok = run(["bash", "scripts/deploy.sh", TV_IP, TV_SSH_PORT, "--reuse"], cwd=STAGE) == 0
    job_tv_status()
    return ok


def job_deploy_update():
    if not require_tv() or not prepare_stage() or not tv_reachable():
        return False
    if not tv_installed():
        log("Auf dem TV ist noch kein Dienst installiert. Erst 'Erstinstallation'.")
        return False
    log("Update ueber upstream 'make deploy-bin' (config.json bleibt, Vorversion wird gesichert).")
    ok = run(["make", "deploy-bin", "TV_IP=" + TV_IP, "SSH_PORT=" + TV_SSH_PORT], cwd=STAGE) == 0
    job_tv_status()
    return ok


def job_rollback():
    if not require_tv() or not tv_reachable():
        return False
    # Befehl aus docs/operations.md (Rollback)
    remote = ("systemctl stop lg-hue-sync && cp {d}/lg-hue-sync.previous {d}/lg-hue-sync && "
              "chmod 755 {d}/lg-hue-sync && systemctl start lg-hue-sync").format(d=REMOTE_DIR)
    ok = run(ssh_cmd(remote)) == 0
    job_tv_status()
    return ok


def job_test_ssh():
    if not require_tv():
        return False
    ok = tv_reachable()
    if ok:
        _, out, _ = tv_ssh("uname -m; cat /etc/issue 2>/dev/null | head -n 1")
        log("SSH ok: " + " | ".join(out.split("\n")).strip(" |"))
    return ok


def job_install_key(password):
    if not require_tv():
        return False
    key = public_key()
    if not key or "'" in key:
        log("Kein gueltiger oeffentlicher Schluessel vorhanden.")
        return False
    if not password:
        log("Kein Passwort angegeben.")
        return False
    remote = ("umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; "
              "grep -qxF '{k}' ~/.ssh/authorized_keys || echo '{k}' >> ~/.ssh/authorized_keys").format(k=key)
    cmd = ["sshpass", "-e", "ssh", "-p", TV_SSH_PORT,
           "-o", "BatchMode=no", "-o", "PubkeyAuthentication=no",
           "-o", "PreferredAuthentications=password,keyboard-interactive",
           "root@" + TV_IP, remote]
    if run(cmd, env={"SSHPASS": password}) != 0:
        log("Key-Installation fehlgeschlagen (Passwort falsch oder Passwort-Login deaktiviert?).")
        return False
    return job_test_ssh()


def job_build_deploy():
    return job_build() and job_deploy_update()


def job_auto():
    if not job_check():
        return False
    state = load_state()
    if state.get("remote_commit") == state.get("built_commit"):
        return True
    if not AUTO_BUILD:
        log("AUTO_BUILD=false: Update wird nur angezeigt.")
        return True
    if not job_build():
        return False
    if not AUTO_DEPLOY:
        log("AUTO_DEPLOY=false: neues Binary liegt bereit, Senden per Button.")
        return True
    if require_tv() and tv_reachable() and tv_installed():
        return job_deploy_update()
    log("AUTO_DEPLOY: TV nicht erreichbar oder nicht installiert, uebersprungen.")
    return True


JOBS = {
    "check": job_check,
    "build": job_build,
    "deploy_first": job_deploy_first,
    "deploy_update": job_deploy_update,
    "build_deploy": job_build_deploy,
    "rollback": job_rollback,
    "tv_status": job_tv_status,
    "test_ssh": job_test_ssh,
    "install_key": job_install_key,
    "auto": job_auto,
}


def start_job(name, payload=None):
    fn = JOBS.get(name)
    if fn is None:
        return False, "unbekannter Job"
    if not _job_lock.acquire(blocking=False):
        return False, "es laeuft bereits: %s" % _current["name"]

    def worker():
        _current["name"] = name
        _current["started"] = now_iso()
        rotate_log()
        log("========== %s ==========" % name)
        ok = False
        try:
            if name == "install_key":
                ok = fn((payload or {}).get("password", ""))
            else:
                ok = bool(fn())
        except Exception as exc:  # noqa: BLE001
            log("Fehler: %r" % exc)
        finally:
            update_state(last_job=name, last_result="ok" if ok else "fehler", last_finished=now_iso())
            log("========== %s: %s ==========" % (name, "OK" if ok else "FEHLER"))
            _current["name"] = None
            _current["started"] = None
            _job_lock.release()

    threading.Thread(target=worker, daemon=True).start()
    return True, "gestartet"


def scheduler():
    if CHECK_INTERVAL_HOURS <= 0:
        return
    time.sleep(30)
    while True:
        start_job("auto")
        time.sleep(max(300.0, CHECK_INTERVAL_HOURS * 3600))


# --------------------------------------------------------------------------- http

def state_payload():
    state = load_state()
    remote = state.get("remote_commit") or ""
    built = state.get("built_commit") or ""
    tv_sha = state.get("tv_sha256") or ""
    built_sha = state.get("built_sha256") or ""
    state.update({
        "busy": _current["name"],
        "busy_since": _current["started"],
        "update_available": bool(remote) and remote != built,
        "tv_matches_build": bool(tv_sha) and tv_sha == built_sha,
        "pubkey": public_key(),
        "config": {
            "tv_ip": TV_IP, "tv_ssh_port": TV_SSH_PORT, "tv_dashboard_port": TV_DASH_PORT,
            "repo": REPO_URL, "branch": BRANCH, "check_interval_hours": CHECK_INTERVAL_HOURS,
            "auto_build": AUTO_BUILD, "auto_deploy": AUTO_DEPLOY,
        },
    })
    return state


class Handler(BaseHTTPRequestHandler):
    server_version = "lg-hue-sync-deployer"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if not UI_TOKEN:
            return True
        query = parse_qs(urlparse(self.path).query)
        token = self.headers.get("X-Token") or (query.get("k") or [""])[0]
        return hmac.compare_digest(token, UI_TOKEN)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if path in ("/", "/index.html"):
            with open(os.path.join(APP_DIR, "index.html"), "rb") as handle:
                return self._send(200, handle.read(), "text/html; charset=utf-8")
        if not self._authorized():
            return self._send(401, {"error": "token"})
        if path == "/api/state":
            return self._send(200, state_payload())
        if path == "/api/log":
            return self._send(200, log_tail(), "text/plain; charset=utf-8")
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._authorized():
            return self._send(401, {"error": "token"})
        # Custom-Header erzwingt CORS-Preflight -> fremde Webseiten koennen keine Jobs ausloesen.
        if self.headers.get("X-Requested-With") != "deployer":
            return self._send(403, {"error": "header"})
        if not path.startswith("/api/job/"):
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        payload = {}
        if length:
            try:
                payload = json.loads(self.rfile.read(min(length, 8192)).decode("utf-8"))
            except ValueError:
                return self._send(400, {"error": "json"})
        ok, message = start_job(path[len("/api/job/"):], payload)
        return self._send(202 if ok else 409, {"ok": ok, "message": message})


def main():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    ensure_ssh()
    if _current["name"] is None:
        update_state(booted_at=now_iso())
    threading.Thread(target=scheduler, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", WEBUI_PORT), Handler)
    log("Web-UI auf Port %d, TV=%s, Upstream=%s@%s" % (WEBUI_PORT, TV_IP or "-", REPO_URL, BRANCH))
    server.serve_forever()


if __name__ == "__main__":
    main()
