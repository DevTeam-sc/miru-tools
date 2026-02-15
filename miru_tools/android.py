import os
import subprocess
import time
from typing import Callable, List, Optional

from miru_tools.assets import AssetsError, ensure_asset_cached, load_manifest, select_asset


class AdbError(RuntimeError):
    pass


def _run_adb(
    adb: str,
    serial: Optional[str],
    args: List[str],
    *,
    check: bool = True,
    timeout_s: int = 120,
) -> subprocess.CompletedProcess:
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    cmd += args

    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
    )
    if check and proc.returncode != 0:
        raise AdbError(proc.stdout.strip() or f"adb failed with exit code {proc.returncode}")
    return proc


def _list_adb_devices(adb: str) -> List[str]:
    proc = _run_adb(adb, None, ["devices"], check=True, timeout_s=30)
    serials: List[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices attached"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def _select_adb_serial(adb: str) -> str:
    preferred = os.environ.get("MIRU_ADB_SERIAL") or os.environ.get("ANDROID_SERIAL")
    devices = _list_adb_devices(adb)

    if preferred:
        if preferred not in devices:
            devices_desc = ", ".join(devices) if devices else "none"
            raise AdbError(f"ADB device '{preferred}' not found (connected: {devices_desc})")
        return preferred

    if len(devices) == 0:
        raise AdbError("No Android device detected via adb. Check `adb devices`.")
    if len(devices) > 1:
        raise AdbError("Multiple adb devices detected. Set MIRU_ADB_SERIAL or ANDROID_SERIAL.")

    return devices[0]


def _map_android_abi_to_arch(abi: str) -> str:
    a = (abi or "").strip()
    if a in ("arm64-v8a", "armeabi-v7a", "x86_64", "x86"):
        return a
    if a.startswith("arm64"):
        return "arm64-v8a"
    if a.startswith("armeabi"):
        return "armeabi-v7a"
    if a.startswith("x86_64"):
        return "x86_64"
    if a.startswith("x86"):
        return "x86"
    raise AdbError(f"Unsupported Android ABI: {a!r}")


def ensure_android_server_running_via_adb(
    *,
    version: str,
    port: int,
    status_cb: Optional[Callable[[str], None]] = None,
) -> None:
    def status(msg: str) -> None:
        if status_cb is not None:
            status_cb(msg)

    adb = os.environ.get("MIRU_ADB", "adb")
    serial = _select_adb_serial(adb)

    status(f"adb: using device {serial}")

    abi = _run_adb(adb, serial, ["shell", "getprop", "ro.product.cpu.abi"], timeout_s=30).stdout.strip()
    if not abi:
        raise AdbError("Failed to determine device ABI (ro.product.cpu.abi)")
    arch = _map_android_abi_to_arch(abi)

    status(f"assets: resolving android/{arch} miru-server for v{version}")
    try:
        manifest = load_manifest(version)
        asset = select_asset(manifest, kind="server", platform_name="android", arch=arch)
        server_path = ensure_asset_cached(version, asset)
    except AssetsError as e:
        raise AdbError(str(e)) from e

    status("adb: pushing miru-server to /data/local/tmp/miru-server")
    _run_adb(adb, serial, ["push", str(server_path), "/data/local/tmp/miru-server"], timeout_s=600)
    _run_adb(adb, serial, ["shell", "chmod", "755", "/data/local/tmp/miru-server"], check=False, timeout_s=30)

    status("adb: stopping any existing miru-server")
    _run_adb(
        adb,
        serial,
        ["shell", "sh", "-c", "toybox killall miru-server 2>/dev/null || killall miru-server 2>/dev/null || true"],
        check=False,
        timeout_s=30,
    )

    root_probe = _run_adb(adb, serial, ["shell", "su", "-c", "id"], check=False, timeout_s=15)
    have_root = root_probe.returncode == 0 and "uid=0" in (root_probe.stdout or "")

    launch_cmd = f"/data/local/tmp/miru-server -l 0.0.0.0:{port} >/dev/null 2>&1 &"
    if have_root:
        status(f"adb: starting miru-server as root on 0.0.0.0:{port}")
        _run_adb(adb, serial, ["shell", "su", "-c", launch_cmd], timeout_s=30)
    else:
        status(f"adb: starting miru-server as shell on 0.0.0.0:{port} (no root)")
        _run_adb(adb, serial, ["shell", "sh", "-c", launch_cmd], timeout_s=30)

    status(f"adb: forwarding tcp:{port} -> tcp:{port}")
    _run_adb(adb, serial, ["forward", f"tcp:{port}", f"tcp:{port}"], check=False, timeout_s=30)

    status("adb: verifying miru-server is running")
    for attempt in range(5):
        pid = _run_adb(
            adb,
            serial,
            ["shell", "sh", "-c", "pidof miru-server 2>/dev/null || toybox pidof miru-server 2>/dev/null || true"],
            check=False,
            timeout_s=30,
        ).stdout.strip()
        if pid:
            return
        time.sleep(0.2 + (attempt * 0.2))

    ps_out = _run_adb(adb, serial, ["shell", "ps", "-A"], check=False, timeout_s=30).stdout
    if "miru-server" not in (ps_out or ""):
        raise AdbError("miru-server did not start (pidof empty)")
