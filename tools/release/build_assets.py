import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

MANIFEST_FILENAME = "miru-assets-manifest.json"
HASHES_FILENAME = "hashes.sha256.txt"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_asset(src: Path, dst: Path) -> Tuple[str, int]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    sha256 = _sha256_file(dst)
    size = dst.stat().st_size
    return sha256, size


def _add_asset(
    manifest: Dict[str, Any],
    *,
    kind: str,
    platform_name: str,
    arch: str,
    filename: str,
    sha256: str,
    size: int,
) -> None:
    manifest.setdefault("assets", []).append(
        {
            "kind": kind,
            "platform": platform_name,
            "arch": arch,
            "filename": filename,
            "sha256": sha256,
            "size": size,
        }
    )


def _parse_android_spec(raw: str) -> Tuple[Path, str]:
    """
    Parse a spec formatted like:
      C:\\path\\to\\build-android-arm64:arm64-v8a

    Note: Windows paths contain ':' after the drive letter, so we split on the last ':'.
    """
    if ":" not in raw:
        raise ValueError("expected --android BUILD_DIR:ARCH, e.g. build-android-arm64:arm64-v8a")
    builddir_raw, arch = raw.rsplit(":", 1)
    builddir = Path(builddir_raw)
    arch = arch.strip()
    if not arch:
        raise ValueError("android arch must be non-empty, e.g. arm64-v8a")
    return builddir, arch


def _add_android_assets(
    manifest: Dict[str, Any], hashes: List[str], *, builddir: Path, arch: str, outdir: Path
) -> None:
    # Android server
    server_src = builddir / "subprojects" / "miru-core" / "server" / "miru-server"
    server_name = f"miru-server-android-{arch}"
    sha256, size = _copy_asset(server_src, outdir / server_name)
    _add_asset(
        manifest,
        kind="server",
        platform_name="android",
        arch=arch,
        filename=server_name,
        sha256=sha256,
        size=size,
    )
    hashes.append(f"{sha256}  {server_name}")

    # Android inject
    inject_src = builddir / "subprojects" / "miru-core" / "inject" / "miru-inject"
    inject_name = f"miru-inject-android-{arch}"
    sha256, size = _copy_asset(inject_src, outdir / inject_name)
    _add_asset(
        manifest,
        kind="inject",
        platform_name="android",
        arch=arch,
        filename=inject_name,
        sha256=sha256,
        size=size,
    )
    hashes.append(f"{sha256}  {inject_name}")

    # Android gadget
    gadget_src = builddir / "subprojects" / "miru-core" / "lib" / "gadget" / "miru-gadget.so"
    gadget_name = f"miru-gadget-android-{arch}.so"
    sha256, size = _copy_asset(gadget_src, outdir / gadget_name)
    _add_asset(
        manifest,
        kind="gadget",
        platform_name="android",
        arch=arch,
        filename=gadget_name,
        sha256=sha256,
        size=size,
    )
    hashes.append(f"{sha256}  {gadget_name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Package Miru GitHub Release assets + manifest")
    parser.add_argument("--version", required=True, help="Miru version, e.g. 16.5.7")
    parser.add_argument(
        "--android",
        action="append",
        default=[],
        metavar="BUILD_DIR:ARCH",
        help="Android build dir and arch label, e.g. build-android-arm64:arm64-v8a (repeatable)",
    )
    parser.add_argument("--outdir", default="dist/release", help="Output directory (default: dist/release)")
    args = parser.parse_args()

    version = args.version.strip()
    outdir = Path(args.outdir) / f"v{version}"
    outdir.mkdir(parents=True, exist_ok=True)

    manifest: Dict[str, Any] = {"version": version, "assets": []}
    hashes: List[str] = []

    for raw in args.android:
        builddir, arch = _parse_android_spec(raw)
        _add_android_assets(manifest, hashes, builddir=builddir, arch=arch, outdir=outdir)

    (outdir / MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    (outdir / HASHES_FILENAME).write_text("\n".join(hashes) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
