import os
import shutil
import sys
from pathlib import Path
from typing import Iterator, List

from setuptools import setup

SOURCE_ROOT = Path(__file__).resolve().parent

pkg_info = SOURCE_ROOT / "PKG-INFO"
in_source_package = pkg_info.exists()


def main():
    version = detect_version()
    setup(
        name="miru-tools",
        version=version,
        description="Miru CLI tools",
        long_description="CLI tools for [Miru](https://miru.re).",
        long_description_content_type="text/markdown",
        author="Miru Developers",
        author_email="oleavr@miru.re",
        url="https://miru.re",
        python_requires=">=3.9",
        install_requires=[
            "colorama >= 0.2.7, < 1.0.0",
            f"miru-core == {version}",
            "prompt-toolkit >= 2.0.0, < 4.0.0",
            "pygments >= 2.0.2, < 3.0.0",
            "websockets >= 13.0.0, < 14.0.0",
        ],
        license="wxWindows Library Licence, Version 3.1",
        zip_safe=False,
        keywords="miru debugger dynamic instrumentation inject javascript windows macos linux ios iphone ipad android qnx",
        classifiers=[
            "Development Status :: 5 - Production/Stable",
            "Environment :: Console",
            "Environment :: MacOS X",
            "Environment :: Win32 (MS Windows)",
            "Intended Audience :: Developers",
            "Intended Audience :: Science/Research",
            "License :: OSI Approved",
            "Natural Language :: English",
            "Operating System :: MacOS :: MacOS X",
            "Operating System :: Microsoft :: Windows",
            "Operating System :: POSIX :: Linux",
            "Programming Language :: Python :: 3",
            "Programming Language :: Python :: 3.9",
            "Programming Language :: Python :: 3.10",
            "Programming Language :: Python :: 3.11",
            "Programming Language :: Python :: 3.12",
            "Programming Language :: JavaScript",
            "Topic :: Software Development :: Debuggers",
            "Topic :: Software Development :: Libraries :: Python Modules",
        ],
        packages=["miru_tools"],
        package_data={
            "miru_tools": fetch_built_assets(),
        },
        entry_points={
            "console_scripts": [
                "miru = miru_tools.repl:main",
                "miru-ls-devices = miru_tools.lsd:main",
                "miru-ps = miru_tools.ps:main",
                "miru-kill = miru_tools.kill:main",
                "miru-ls = miru_tools.ls:main",
                "miru-rm = miru_tools.rm:main",
                "miru-pull = miru_tools.pull:main",
                "miru-push = miru_tools.push:main",
                "miru-discover = miru_tools.discoverer:main",
                "miru-trace = miru_tools.tracer:main",
                "miru-itrace = miru_tools.itracer:main",
                "miru-join = miru_tools.join:main",
                "miru-create = miru_tools.creator:main",
                "miru-compile = miru_tools.compiler:main",
                "miru-pm = miru_tools.pm:main",
                "miru-apk = miru_tools.apk:main",
            ]
        },
    )


def detect_version() -> str:
    if in_source_package:
        version_line = [
            line for line in pkg_info.read_text(encoding="utf-8").split("\n") if line.startswith("Version: ")
        ][0].strip()
        version = version_line[9:]
    else:
        version = os.environ.get("MIRU_VERSION")
        if version is not None:
            return version

        releng_location = next(enumerate_releng_locations(), None)
        if releng_location is not None:
            sys.path.insert(0, str(releng_location.parent))
            try:
                from releng.miru_version import detect
            except ImportError:
                from releng.frida_version import detect

            version = detect(SOURCE_ROOT).name.replace("-dev.", ".dev")
        else:
            version = "0.0.0"
    return version


def fetch_built_assets() -> List[str]:
    pkgdir = SOURCE_ROOT / "miru_tools"
    assets = set()

    # Always include any already-present assets (important for VCS builds where PKG-INFO is absent).
    assets.update([f.name for f in pkgdir.glob("*_agent.js")])
    assets.update([f.name for f in pkgdir.glob("*.zip")])

    # Ensure bridges are available in-package at runtime.
    src_bridges = SOURCE_ROOT / "bridges"
    bridges_dir = pkgdir / "bridges"
    if src_bridges.exists():
        bridges_dir.mkdir(exist_ok=True)
        for f in src_bridges.glob("*.js"):
            shutil.copy(f, bridges_dir)
            assets.add((Path("bridges") / f.name).as_posix())

    if bridges_dir.exists():
        assets.update([f.relative_to(pkgdir).as_posix() for f in bridges_dir.glob("*.js")])

    if not in_source_package:
        agents_builddir = SOURCE_ROOT / "build" / "agents"
        if agents_builddir.exists():
            for child in agents_builddir.iterdir():
                if child.is_dir():
                    for f in child.glob("*_agent.js"):
                        shutil.copy(f, pkgdir)
                        assets.add(f.name)

        bridges_builddir = SOURCE_ROOT / "build" / "bridges"
        if bridges_builddir.exists():
            bridges_dir.mkdir(exist_ok=True)
            for f in bridges_builddir.glob("*.js"):
                shutil.copy(f, bridges_dir)
                assets.add((Path("bridges") / f.name).as_posix())

        apps_builddir = SOURCE_ROOT / "build" / "apps"
        if apps_builddir.exists():
            for child in apps_builddir.iterdir():
                if child.is_dir():
                    for f in child.glob("*.zip"):
                        shutil.copy(f, pkgdir)
                        assets.add(f.name)

    return sorted(assets)


def enumerate_releng_locations() -> Iterator[Path]:
    val = os.environ.get("MESON_SOURCE_ROOT")
    if val is not None:
        parent_releng = Path(val) / "releng"
        if releng_location_exists(parent_releng):
            yield parent_releng

    local_releng = SOURCE_ROOT / "releng"
    if releng_location_exists(local_releng):
        yield local_releng


def releng_location_exists(location: Path) -> bool:
    return (location / "miru_version.py").exists() or (location / "frida_version.py").exists()


if __name__ == "__main__":
    main()
