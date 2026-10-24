#!/usr/bin/env python3
"""Launch source-pinned durable acceptance containers after candidate provisioning."""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
PYTHON = "/app/.venv/bin/python"


def docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()


def write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def freeze_tools(root):
    names = (
        "__init__.py", "acceptance/__init__.py", "acceptance/run_active_acceptance.py",
        "acceptance/observe_candidate_soak.py", "acceptance/observe_second_acceptance.py",
    )
    manifest = {name: hashlib.sha256((ROOT / "backend/scripts" / name).read_bytes()).hexdigest()
                for name in names}
    fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    bundle = root / "observer-code" / fingerprint[:16] / "scripts"
    if bundle.exists():
        for name, digest in manifest.items():
            if hashlib.sha256((bundle / name).read_bytes()).hexdigest() != digest:
                raise ValueError("observer source bundle changed")
        return bundle
    bundle.mkdir(parents=True)
    for name in names:
        source = ROOT / "backend/scripts" / name
        (bundle / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, bundle / name)
        if hashlib.sha256((bundle / name).read_bytes()).hexdigest() != manifest[name]:
            raise ValueError("observer changed during snapshot")
    write(bundle.parent / "manifest.json", manifest)
    return bundle


def launch(root, deployment, name, module, args, memory, *, formal=False, socket=False, load=False):
    bundle = freeze_tools(root)
    project = deployment["project"]
    image = deployment["build"]["artifacts"]["runtime"]["id"]
    target = "/acceptance/observations/" + name + "/status.json"
    health = (
        "import json,time;from pathlib import Path;"
        f"p=Path({target!r});r=json.loads(p.read_text());"
        "assert r['status']=='succeeded' or (r['status'] not in "
        "['failed','interrupted','skipped_prerequisite'] and time.time()-p.stat().st_mtime<120)"
    )
    command = [
        "create", "--name", project + "-" + name, "--network", project + "_default",
        "--memory", memory, "--cpus", "1", "--read-only", "--security-opt", "no-new-privileges:true",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
        "--log-driver", "local", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
        "--restart", "unless-stopped" if formal else "no",
        "--health-cmd", shlex.join([PYTHON, "-c", health]),
        "--health-interval", "30s", "--health-timeout", "5s", "--health-start-period", "120s",
        "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "POLARS_MAX_THREADS=2",
        "-e", "OPENBLAS_NUM_THREADS=1", "-e", "OMP_NUM_THREADS=1",
        "-v", f"{root}:/acceptance", "-v", f"{bundle}:/app/scripts:ro",
    ]
    if socket:
        command += ["-v", "/var/run/docker.sock:/var/run/docker.sock:ro"]
    if formal:
        wrapper = (
            "import runpy,sys,threading;"
            "module=sys.argv.pop(1);sys.argv[0]=module;"
            "runpy.run_module(module,run_name='__main__');threading.Event().wait()"
        )
        command += ["--entrypoint", PYTHON, image, "-c", wrapper, module, *args]
    else:
        command += ["--entrypoint", PYTHON, image, "-m", module, *args]
    cid = docker(*command)
    if load:
        docker("network", "connect", project + "_edge", cid)
        docker("network", "connect", project + "_pg", cid)
    docker("start", cid)
    return {"container": project + "-" + name, "id": cid, "memory": memory,
            "module": module, "args": args, "restart": "unless-stopped" if formal else "no",
            "observer_source": str(bundle)}


def arm(root, image, module, args):
    docker("run", "--rm", "--network", "none", "--memory", "256m",
           "-e", "POLARS_MAX_THREADS=2", "-e", "PYTHONDONTWRITEBYTECODE=1",
           "-v", f"{root}:/acceptance", "-v", f"{freeze_tools(root)}:/app/scripts:ro",
           "--entrypoint", PYTHON, image, "-m", module, *args, "--arm")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--formal", action="store_true")
    parser.add_argument("--smoke-name", default="smoke")
    args = parser.parse_args()
    if args.smoke == args.formal:
        parser.error("select exactly one of --smoke or --formal")
    if not args.smoke_name.replace("-", "").isalnum():
        parser.error("smoke-name must contain only letters, digits or hyphens")
    root = args.run_dir.resolve()
    deployment = json.loads((root / "deployment.json").read_text())
    freeze_tools(root)
    observations = root / "observations"
    observations.mkdir(exist_ok=True)
    if args.smoke:
        result = launch(root, deployment, args.smoke_name, "scripts.acceptance.run_active_acceptance",
                        ["--run-dir", "/acceptance/observations/" + args.smoke_name, "--hours", str(5 / 60)],
                        "256m", load=True)
        write(root / f"{args.smoke_name}-launch.json", result)
        print(json.dumps(result))
        return
    smoke = json.loads((observations / args.smoke_name / "report.json").read_text())
    if smoke["status"] != "succeeded":
        raise ValueError("100-user mixed-load smoke must pass before formal clocks start")
    smoke_launch = json.loads((root / f"{args.smoke_name}-launch.json").read_text())
    if smoke_launch["observer_source"] != str(freeze_tools(root)):
        raise ValueError("observer source changed since the successful smoke")
    for service in deployment["services"].values():
        state = json.loads(docker("inspect", service["id"]))[0]["State"]
        if state["Health"]["Status"] != "healthy":
            raise ValueError("candidate is not fully healthy")
    active = launch(root, deployment, "active72", "scripts.acceptance.run_active_acceptance",
                    ["--run-dir", "/acceptance/observations/active72", "--hours", "72"],
                    "256m", formal=True, load=True)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        path = observations / "active72/status.json"
        state = json.loads(path.read_text()) if path.exists() else {}
        if state.get("status") == "running":
            break
        if state.get("status") == "failed":
            raise ValueError("formal load failed during warmup")
        time.sleep(5)
    else:
        raise TimeoutError("formal load did not enter running state")
    image = deployment["build"]["artifacts"]["runtime"]["id"]
    soak_args = ["--run-dir", "/acceptance/observations/passive72"]
    arm(root, image, "scripts.acceptance.observe_candidate_soak", [
        *soak_args, "--duration-hours", "72", "--interval-seconds", "30",
        *[arg for role in ("api", "platform", "gateway")
          for arg in ("--endpoint", f"{role}=http://{role}:3018/health/ready")],
        *[arg for service in deployment["services"].values() for arg in ("--container", service["name"])],
        "--disk-path", "/acceptance",
    ])
    passive = launch(root, deployment, "passive72", "scripts.acceptance.observe_candidate_soak",
                     soak_args, "128m", formal=True, socket=True)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    day = now.date() + (timedelta(days=1) if (now.hour, now.minute) >= (9, 15) else timedelta())
    while day.weekday() >= 5:
        day += timedelta(days=1)
    market_args = ["--run-dir", "/acceptance/observations/tradingday",
                   "--health-url", "http://platform:3018/health/ready"]
    arm(root, image, "scripts.acceptance.observe_second_acceptance", [
        *market_args, "--data-dir", "/acceptance/platform",
        "--preferences-path", "/acceptance/config/preferences.json",
        "--date", day.isoformat(), "--profile", "full-trading-day",
    ])
    market = launch(root, deployment, "tradingday", "scripts.acceptance.observe_second_acceptance",
                    market_args, "256m", formal=True)
    result = {"started_at": now.isoformat(), "active": active, "passive": passive, "market": market,
              "active_window_start": state["window_start"], "active_window_end": state["window_end"],
              "candidate_trading_date": day.isoformat()}
    if Path("/usr/bin/caffeinate").exists():
        # Agent terminal cleanup can kill detached child processes. Let
        # launchd own the time-bounded sleep assertion across terminal exits.
        label = "org.tsp.acceptance." + deployment["project"].removeprefix("tsp-soak-") + "-awake"
        seconds = 72 * 3600 + 600
        subprocess.run(
            ["/bin/launchctl", "submit", "-l", label, "--",
             "/usr/bin/caffeinate", "-i", "-t", str(seconds)],
            check=True, capture_output=True, text=True,
        )
        result["idle_sleep_inhibitor"] = {
            "manager": "launchd", "label": label, "seconds": seconds,
        }
    result["launcher_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    write(root / "formal-launch.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
