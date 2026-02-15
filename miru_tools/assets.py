import hashlib
import json
import os
import platform
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

MANIFEST_FILENAME = "miru-assets-manifest.json"

# Default GitHub Releases base URL (override with MIRU_ASSETS_BASE_URL).
DEFAULT_ASSETS_BASE_URL_TEMPLATE = "https://github.com/DevTeam-sc/miru-tools/releases/download/v{version}/"


class AssetsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Asset:
    kind: str
    platform: str
    arch: str
    filename: str
    sha256: str
    size: Optional[int] = None


def get_cache_dir() -> Path:
    override = os.environ.get("MIRU_ASSETS_CACHE_DIR")
    if override:
        return Path(override).expanduser()

    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA")
        base_dir = Path(base) if base else (Path.home() / "AppData" / "Local")
        return base_dir / "miru" / "Cache"
    if system == "Darwin":
        return Path.home() / "Library" / "Caches" / "miru"

    base = os.environ.get("XDG_CACHE_HOME")
    base_dir = Path(base) if base else (Path.home() / ".cache")
    return base_dir / "miru"


def get_assets_base_url(version: str) -> str:
    override = os.environ.get("MIRU_ASSETS_BASE_URL")
    if override:
        return override.rstrip("/") + "/"

    template = DEFAULT_ASSETS_BASE_URL_TEMPLATE
    if "<ORG>" in template or "<REPO>" in template:
        raise AssetsError(
            "MIRU_ASSETS_BASE_URL is not set. Set it to your GitHub Releases base URL, e.g. "
            f"'https://github.com/ORG/REPO/releases/download/v{version}/'."
        )

    return template.format(version=version).rstrip("/") + "/"


def _iter_file_chunks(path: Path, chunk_size: int = 1024 * 1024) -> Iterable[bytes]:
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            yield chunk


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    for chunk in _iter_file_chunks(path):
        h.update(chunk)
    return h.hexdigest()


def _http_get(url: str, timeout_s: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "miru-tools"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        raise AssetsError(f"Download failed: HTTP {e.code} {e.reason}") from e
    except urllib.error.URLError as e:
        raise AssetsError(f"Download failed: {e.reason}") from e


def _http_download(url: str, dest: Path, timeout_s: int = 300) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "miru-tools"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as f:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
    except urllib.error.HTTPError as e:
        raise AssetsError(f"Download failed: HTTP {e.code} {e.reason}") from e
    except urllib.error.URLError as e:
        raise AssetsError(f"Download failed: {e.reason}") from e


def load_manifest(version: str) -> Dict[str, Any]:
    cache_path = get_cache_dir() / "assets" / version / MANIFEST_FILENAME
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            try:
                cache_path.unlink()
            except Exception:
                pass

    base_url = get_assets_base_url(version)
    manifest_url = base_url + MANIFEST_FILENAME
    raw = _http_get(manifest_url)
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise AssetsError("Invalid assets manifest JSON") from e

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return manifest


def parse_asset(entry: Dict[str, Any]) -> Asset:
    try:
        kind = str(entry["kind"])
        platform_name = str(entry["platform"])
        arch = str(entry["arch"])
        filename = str(entry["filename"])
        sha256 = str(entry["sha256"])
        size = entry.get("size")
        if size is not None:
            size = int(size)
    except Exception as e:
        raise AssetsError("Invalid asset entry in manifest") from e

    sha256_norm = sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha256_norm):
        raise AssetsError(f"Invalid sha256 in manifest for {filename!r}")

    return Asset(
        kind=kind,
        platform=platform_name,
        arch=arch,
        filename=filename,
        sha256=sha256_norm,
        size=size,
    )


def _asset_cache_path(version: str, asset: Asset) -> Path:
    return get_cache_dir() / "assets" / version / asset.platform / asset.arch / asset.filename


def ensure_asset_cached(version: str, asset: Asset) -> Path:
    cache_path = _asset_cache_path(version, asset)
    if cache_path.exists():
        try:
            if _sha256_file(cache_path) == asset.sha256:
                return cache_path
        except Exception:
            pass
        try:
            cache_path.unlink()
        except Exception:
            pass

    base_url = get_assets_base_url(version)
    url = base_url + asset.filename

    tmp = cache_path.with_suffix(cache_path.suffix + ".part")
    if tmp.exists():
        try:
            tmp.unlink()
        except Exception:
            pass

    _http_download(url, tmp)

    actual = _sha256_file(tmp)
    if actual != asset.sha256:
        try:
            tmp.unlink()
        except Exception:
            pass
        raise AssetsError(f"SHA256 mismatch for {asset.filename}: expected {asset.sha256} got {actual}")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp.replace(cache_path)
    return cache_path


def select_asset(manifest: Dict[str, Any], *, kind: str, platform_name: str, arch: str) -> Asset:
    entries = manifest.get("assets", [])
    if not isinstance(entries, list):
        raise AssetsError("Invalid manifest: 'assets' must be a list")

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        asset = parse_asset(entry)
        if asset.kind == kind and asset.platform == platform_name and asset.arch == arch:
            return asset

    raise AssetsError(f"No asset found for kind={kind!r} platform={platform_name!r} arch={arch!r}")
