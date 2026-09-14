from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows installer test")


def _program_files_git() -> Path | None:
    roots = {
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
    }
    for root in filter(None, roots):
        for relative in (Path("Git/cmd/git.exe"), Path("Git/bin/git.exe")):
            candidate = Path(root) / relative
            if candidate.is_file():
                return candidate
    return None


def test_installer_rerun_whatif_allows_only_existing_attestation_and_ignores_path(
    tmp_path,
):
    git = _program_files_git()
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if git is None or not powershell.is_file():
        pytest.skip("signed Git for Windows and Windows PowerShell are required")

    install_root = tmp_path / "HermesLab"
    runtime = install_root / "runtime"
    runtime.mkdir(parents=True)
    subprocess.run([git, "init", "--quiet", runtime], check=True)
    subprocess.run([git, "-C", runtime, "config", "user.name", "Jarvs Test"], check=True)
    subprocess.run(
        [git, "-C", runtime, "config", "user.email", "jarvs-test@example.invalid"],
        check=True,
    )
    tracked = runtime / "tracked.txt"
    tracked.write_text("fixture", encoding="utf-8")
    subprocess.run([git, "-C", runtime, "add", "tracked.txt"], check=True)
    subprocess.run([git, "-C", runtime, "commit", "--quiet", "-m", "fixture"], check=True)

    attestation = runtime / ".jarvs-hermes-attestation.json"
    attestation.write_text('{"previous":"attestation"}', encoding="utf-8")
    before = (attestation.read_bytes(), attestation.stat().st_mtime_ns)

    fake_path = tmp_path / "path-shim"
    fake_path.mkdir()
    shutil.copy2(Path(os.environ["SystemRoot"]) / "System32/where.exe", fake_path / "git.exe")
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join((str(fake_path), environment.get("PATH", "")))
    script = Path(__file__).parents[1] / "scripts/install-hermes.ps1"
    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            script,
            "-InstallRoot",
            install_root,
            "-Force",
            "-WhatIf",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "3c27eb6234bf91b8ceee9e9071591b31e9b148cb" in completed.stdout
    assert (attestation.read_bytes(), attestation.stat().st_mtime_ns) == before
    assert subprocess.run(
        [git, "-C", runtime, "status", "--porcelain", "--untracked-files=normal"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == "?? .jarvs-hermes-attestation.json"
