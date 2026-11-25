#!/usr/bin/env python3
"""The single local TSP production transport; no aliases or legacy fallback."""

import gzip
import http.cookiejar
import ipaddress
import json
import os
import stat
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_PATH = Path.home() / ".config/tsp/production-connection.json"
IDENTITY_REPLY = "ActiveState=active\nFragmentPath=/etc/systemd/system/tsp-full.service\ntsp-full\n"
IDENTITY_COMMAND = (
    "systemctl show tsp-full.service -p ActiveState -p FragmentPath"
    " && docker inspect --format '{{index .Config.Labels \"com.docker.compose.project\"}}'"
    " tsp-full-api-1"
)
RECOVERY_GATE = r"""set -eu
unit=tsp-full.service
[ "$(systemctl show "$unit" -p Id --value)" = "$unit" ]
[ "$(systemctl show "$unit" -p LoadState --value)" = loaded ]
[ "$(systemctl show "$unit" -p FragmentPath --value)" = /etc/systemd/system/tsp-full.service ]
case "$(systemctl show "$unit" -p ExecStart --value)" in
    *"/opt/tsp-full/operations/start_full.py"*) ;;
    *) exit 42 ;;
esac
[ -f /opt/tsp-full/operations/start_full.py ]
[ -f /opt/tsp-full/source/deploy/full/compose.yml ]
api=tsp-full-api-1
ids="$(docker ps -a --filter "name=^/${api}$" --format '{{.ID}}')"
if [ -n "$ids" ]; then
    [ "$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$api")" = tsp-full ]
    [ "$(docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}' "$api")" = api ]
fi
"""
RECOVERY_STATUS = r"""systemctl show tsp-full.service tsp-compute-broker.service -p Id -p ActiveState -p SubState -p Result
docker ps -a --filter label=com.docker.compose.project=tsp-full --format '{{.Names}} {{.Status}}'
"""


class ConnectionError(ValueError):
    """A connection was refused before an application operation."""


def _private_file(path):
    try:
        info = path.lstat()
    except OSError:
        raise ConnectionError("required private configuration file is missing") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ConnectionError("configuration must be an owned regular file")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ConnectionError("private configuration permissions must be 0600")


def load_config():
    _private_file(CONFIG_PATH)
    try:
        values = json.loads(CONFIG_PATH.read_text())
        if not isinstance(values, dict) or values.get("schema") != 1:
            raise ValueError()
        host = str(ipaddress.IPv4Address(values["host"]))
        origin = urllib.parse.urlsplit(values["origin"])
        if (values["host"] != host or values["user"] != "root"
                or origin.scheme not in {"http", "https"}
                or origin.hostname != host or origin.port is not None
                or origin.username or origin.password or origin.path
                or origin.query or origin.fragment):
            raise ValueError()
        for name in ("identity_file", "known_hosts_file"):
            path = Path(values[name])
            if not path.is_absolute():
                raise ValueError()
            _private_file(path)
        pin = values["host_public_key"]
        if not isinstance(pin, str) or not pin.startswith("ssh-ed25519 ") or "\n" in pin:
            raise ValueError()
        expected = f"{host} {pin}\n"
        if Path(values["known_hosts_file"]).read_text() != expected:
            raise ValueError()
        if not all(isinstance(values.get(name), str) and values[name]
                   for name in ("username", "password", "registration_secret")):
            raise ValueError()
    except (KeyError, TypeError, ValueError, OSError):
        raise ConnectionError("invalid production configuration or host pin") from None
    return values


def production_origin():
    return verify_identity()["origin"]


def production_target():
    values = load_config()
    return f"{values['user']}@{values['host']}"


def production_credentials():
    values = load_config()
    return {name: values[name] for name in ("username", "password", "registration_secret")}


def _transport(values, program):
    return [
        program, "-F", "/dev/null", "-i", values["identity_file"],
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={values['known_hosts_file']}",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
    ]


def verify_identity(values=None):
    values = values or load_config()
    command = [*_transport(values, "ssh"),
        f"{values['user']}@{values['host']}", IDENTITY_COMMAND]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.SubprocessError):
        raise ConnectionError("production SSH identity verification failed") from None
    if result.returncode or result.stdout != IDENTITY_REPLY:
        raise ConnectionError("production application identity mismatch")
    return values


def recovery(start=False):
    """Run only the fixed TSP recovery operation, even when its API is down.

    Host key verification is unchanged. Check the installed unit and Compose
    identity in the same remote command before doing anything to the service.
    This path never grants a shell, file transfer, or an arbitrary unit action.
    """
    values = load_config()
    action = 'systemctl start --no-block "$unit"\n' if start else ""
    command = [*_transport(values, "ssh"),
        f"{values['user']}@{values['host']}", RECOVERY_GATE + action + RECOVERY_STATUS]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise ConnectionError("production recovery transport failed") from None
    if result.returncode:
        raise ConnectionError("production recovery identity or operation failed")
    print(result.stdout, end="")
    return 0


def ssh_command(*, tty=False):
    values = verify_identity()
    return _transport(values, "ssh") + (["-tt"] if tty else []) + [
        f"{values['user']}@{values['host']}"]


def scp_command():
    return _transport(verify_identity(), "scp")


def tunnel_command():
    values = dict(verify_identity())
    key = values.get("tunnel_identity_file")
    if not isinstance(key, str) or not Path(key).is_absolute():
        raise ConnectionError("configured production tunnel key is required")
    _private_file(Path(key))
    values["identity_file"] = key
    return [*_transport(values, "ssh"),
        "-N", "-T", "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
        "-L", "127.0.0.1:3039:127.0.0.1:3032",
        f"{values['user']}@{values['host']}"]


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if (old.scheme, old.netloc) != (new.scheme, new.netloc):
            raise ConnectionError("cross-origin production redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ProductionClient:
    def __init__(self):
        values = verify_identity()
        self.origin = values["origin"]
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), SameOriginRedirect(),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.call("/api/auth/login", {
            "email": values["username"], "password": values["password"]})
        if self.call("/api/auth/status").get("authenticated") is not True:
            raise ConnectionError("production login was not authenticated")

    def call(self, path, body=None, timeout=90):
        parsed = urllib.parse.urlsplit(path)
        if (not path.startswith("/") or path.startswith("//") or parsed.scheme
                or parsed.netloc or parsed.fragment or ".." in parsed.path.split("/")):
            raise ConnectionError("invalid production API path")
        request = urllib.request.Request(
            self.origin + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Origin": self.origin, "Content-Type": "application/json",
                     "Accept-Encoding": "gzip"})
        with self.opener.open(request, timeout=timeout) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw)

    def close(self):
        self.call("/api/auth/logout", {})


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args == ["check"]:
            verify_identity()
            client = ProductionClient()
            try:
                health = client.call("/health/ready")
                print(json.dumps({"ssh_identity": "verified", "login": "verified",
                                  "health": health}, ensure_ascii=False))
            finally:
                client.close()
            return 0
        if args == ["tunnel"]:
            return subprocess.call(tunnel_command())
        if args == ["recovery-status"]:
            return recovery()
        if args == ["recover"]:
            return recovery(start=True)
        if len(args) == 2 and args[0] == "ssh" and not args[1].startswith("-"):
            if args[1].split()[0].startswith("root@"):
                raise ConnectionError("SSH target overrides are not supported")
            return subprocess.call([*ssh_command(), args[1]])
        if len(args) == 3 and args[0] == "scp":
            paths = args[1:]
            if sum(path.startswith("remote:/") for path in paths) != 1:
                raise ConnectionError("SCP requires exactly one remote:/ absolute path")
            if any(path.startswith("-") or (":" in path and not path.startswith("remote:/"))
                   for path in paths):
                raise ConnectionError("SCP target overrides are not supported")
            command = scp_command()
            target = production_target()
            paths = [target + path[6:] if path.startswith("remote:/") else path for path in paths]
            return subprocess.call(command + paths)
        raise ConnectionError(
            'usage: tsp-remote check | recovery-status | recover | ssh "command" | scp source destination')
    except ConnectionError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
