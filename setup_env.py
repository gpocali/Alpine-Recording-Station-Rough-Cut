#!/usr/bin/env python3
"""
Cross-platform zero-intervention environment installer and dependency configurator
for Alpine Recording Station Rough Cut.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
VENV_DIR = ROOT_DIR / ".venv"
REQ_FILE = ROOT_DIR / "requirements.txt"


def get_venv_python() -> Path:
    if platform.system().lower() == "windows":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def find_uv() -> str | None:
    uv_bin = shutil.which("uv")
    if uv_bin:
        return uv_bin
    if platform.system().lower() == "windows":
        local_app = os.environ.get("LOCALAPPDATA", "")
        candidate = Path(local_app) / "hermes" / "bin" / "uv.exe"
        if candidate.exists():
            return str(candidate)
    return None


def ensure_virtualenv() -> Path:
    v_py = get_venv_python()
    if v_py.exists():
        print(f"[OK] Virtual environment already present at: {VENV_DIR}")
        return v_py

    print(f"[1/4] Creating virtual environment at {VENV_DIR} ...")
    uv_bin = find_uv()
    if uv_bin:
        subprocess.run([uv_bin, "venv", str(VENV_DIR), "--python", sys.executable], check=True)
    else:
        venv.create(str(VENV_DIR), with_pip=True)
    return v_py


def install_python_packages(venv_python: Path) -> None:
    print("[2/4] Installing Python dependencies from requirements.txt ...")
    uv_bin = find_uv()
    if uv_bin:
        subprocess.run(
            [uv_bin, "pip", "install", "-r", str(REQ_FILE), "--python", str(venv_python)],
            check=True,
        )
    else:
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--upgrade", "pip", "wheel"],
            check=True,
        )
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "-r", str(REQ_FILE)],
            check=True,
        )
    print("[OK] Python dependencies installed.")


def ensure_ffmpeg() -> None:
    print("[3/4] Checking for FFmpeg audio codec support ...")
    if shutil.which("ffmpeg"):
        print("[OK] FFmpeg is installed and on PATH.")
        return

    sys_name = platform.system().lower()
    if sys_name == "windows":
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            winget_dir = Path(local_app) / "Microsoft" / "WinGet" / "Packages"
            if winget_dir.exists():
                found = list(winget_dir.rglob("ffmpeg.exe"))
                if found:
                    print(f"[OK] FFmpeg found via WinGet at: {found[0]}")
                    return
        if shutil.which("winget"):
            print("Installing FFmpeg via winget...")
            subprocess.run(
                [
                    "winget",
                    "install",
                    "--id",
                    "Gyan.FFmpeg",
                    "-e",
                    "--accept-source-agreements",
                    "--accept-package-agreements",
                ],
                check=False,
            )
            return
    elif sys_name == "darwin":
        if shutil.which("brew"):
            print("Installing FFmpeg via Homebrew...")
            subprocess.run(["brew", "install", "ffmpeg"], check=False)
            return
    elif sys_name == "linux":
        if shutil.which("apt-get"):
            print("Attempting FFmpeg install via apt-get...")
            subprocess.run(["sudo", "apt-get", "update", "-y"], check=False)
            subprocess.run(["sudo", "apt-get", "install", "-y", "ffmpeg", "libsndfile1"], check=False)
            return

    print("[NOTE] FFmpeg not found on PATH; WAV/FLAC/OGG work natively via libsndfile.")


def verify_installation(venv_python: Path, preload_model: bool = False) -> None:
    print("[4/4] Verifying core modules and creating workspace folders ...")
    for sub in ("uploads", "sessions", "outputs"):
        (ROOT_DIR / "workspace_data" / sub).mkdir(parents=True, exist_ok=True)
    (ROOT_DIR / "models_cache").mkdir(parents=True, exist_ok=True)

    check_code = (
        "import fastapi, numpy, scipy, soundfile, faster_whisper; "
        "print('[OK] Verified FastAPI, NumPy, SciPy, SoundFile, and Faster-Whisper.')"
    )
    subprocess.run([str(venv_python), "-c", check_code], check=True)

    if preload_model:
        print("Pre-caching faster-whisper 'base.en' model weights ...")
        preload_code = (
            "from backend.stt_engine import _get_whisper_model; "
            "m = _get_whisper_model('base.en'); "
            "print('[OK] Whisper model cached.' if m else '[NOTE] Model will download on first online run.')"
        )
        subprocess.run([str(venv_python), "-c", preload_code], check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Setup Alpine Recording Station Rough Cut")
    parser.add_argument(
        "--preload-model",
        action="store_true",
        help="Download and cache the default faster-whisper base.en model during installation",
    )
    args = parser.parse_args()

    print("==================================================================")
    print("  Alpine Recording Station — Rough Cut Automated Setup")
    print("==================================================================")
    venv_py = ensure_virtualenv()
    install_python_packages(venv_py)
    ensure_ffmpeg()
    verify_installation(venv_py, preload_model=args.preload_model)
    print("==================================================================")
    print("  Setup Complete! Launch the app with:")
    if platform.system().lower() == "windows":
        print("    .\\run.ps1   (or run.bat)")
    else:
        print("    ./run.sh")
    print("==================================================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
