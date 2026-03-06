#!/usr/bin/env python3
"""
PyMesh Chat — Build Script
Produces a single distributable executable for the current platform.

Usage (run from the project root):
    python build.py

Requirements:
    pip install pyinstaller

Output:
    dist/pymesh-chat        (macOS)
    dist/pymesh-chat.exe    (Windows)
"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT     = Path(__file__).parent
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
SPEC     = ROOT / "pymesh.spec"

IS_WINDOWS = platform.system() == "Windows"
IS_MAC     = platform.system() == "Darwin"

BINARY_NAME = "pymesh-chat.exe" if IS_WINDOWS else "pymesh-chat"


def check_pyinstaller() -> None:
    try:
        import PyInstaller
        print(f"  [ok] PyInstaller {PyInstaller.__version__} found.")
    except ImportError:
        print("  [!]  PyInstaller not found. Installing...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller>=6.0"])
        print("  [ok] PyInstaller installed.")


def clean() -> None:
    for d in (DIST_DIR, BUILD_DIR):
        if d.exists():
            shutil.rmtree(d)
            print(f"  [ok] Cleaned {d}")


def build() -> None:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--clean",
        str(SPEC),
    ]
    print(f"\n  Running: {' '.join(cmd)}\n")
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("\n  [ERROR] Build failed. See output above.")
        sys.exit(1)


def report() -> None:
    binary = DIST_DIR / BINARY_NAME
    if binary.exists():
        size_mb = binary.stat().st_size / 1_048_576
        print(f"\n  ✓  Build successful!")
        print(f"     Binary : {binary}")
        print(f"     Size   : {size_mb:.1f} MB")
        print(f"\n  Run it:")
        if IS_WINDOWS:
            print(f'     dist\\pymesh-chat.exe -a YourName -s your-session')
        else:
            print(f'     ./dist/pymesh-chat -a YourName -s your-session')
        print()
    else:
        print(f"\n  [ERROR] Expected binary not found at {binary}")
        sys.exit(1)


def main() -> None:
    print(f"\n  PyMesh Chat — Packaging for {platform.system()} ({platform.machine()})")
    print(f"  {'=' * 50}")
    check_pyinstaller()
    clean()
    build()
    report()


if __name__ == "__main__":
    main()
