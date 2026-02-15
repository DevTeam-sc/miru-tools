import os
import platform
from pathlib import Path
from typing import Optional


def get_config_dir() -> Path:
    system = platform.system()

    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA")
        base_dir = Path(base) if base else (Path.home() / "AppData" / "Local")
    elif system == "Darwin":
        base_dir = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        base_dir = Path(base) if base else (Path.home() / ".config")

    return base_dir / "miru" / "Config"


def _setting_path(name: str) -> Path:
    if not name:
        raise ValueError("setting name must be non-empty")
    if any(sep in name for sep in ("/", "\\", ":")):
        raise ValueError(f"invalid setting name: {name!r}")
    return get_config_dir() / f"{name}.txt"


def read_setting(name: str) -> Optional[str]:
    path = _setting_path(name)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return value or None


def write_setting(name: str, value: str) -> Path:
    path = _setting_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.strip() + "\n", encoding="utf-8")
    return path

