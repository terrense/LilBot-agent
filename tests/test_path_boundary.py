"""Path escape corpus: workspace resolution must reject every outside path."""

from __future__ import annotations

import os
import subprocess

import pytest

from lilbot.sandbox import Sandbox, SandboxError

COMMON = [
    "../outside.txt",
    "sub/../../outside.txt",
    "a/b/../../../outside.txt",
    "./../outside.txt",
    "../../etc/passwd",
]

WINDOWS = [
    "..\\outside.txt",
    "sub\\..\\..\\outside.txt",
    "C:\\Windows\\win.ini",
    "C:..\\outside.txt",
    "\\\\?\\C:\\Windows\\win.ini",
    "D:\\outside.txt",
    "..\\..\\Users\\Public\\secret.txt",
]

POSIX = [
    "/etc/passwd",
    "/tmp/outside.txt",
    "//etc/passwd",
    "///etc/passwd",
    "/root/.ssh/id_rsa",
    "/var/tmp/outside",
    "/proc/self/environ",
]

CASES = COMMON + (WINDOWS if os.name == "nt" else POSIX)


def test_path_corpus_has_12_cases():
    assert len(CASES) == 12


@pytest.mark.parametrize("path", CASES)
def test_path_escape_rejected(tmp_path, path):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises((SandboxError, OSError, ValueError)):
        Sandbox(root).resolve(path)


def test_symlink_escape_rejected(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs admin/dev mode on this platform")
    with pytest.raises(SandboxError):
        Sandbox(root).resolve("link/secret.txt")
    with pytest.raises(SandboxError):
        Sandbox(root).resolve("link")


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-specific")
def test_junction_escape_rejected(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    junction = root / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"could not create a junction: {result.stderr or result.stdout}")
    with pytest.raises(SandboxError):
        Sandbox(root).resolve("junction/secret.txt")
    with pytest.raises(SandboxError):
        Sandbox(root).resolve("junction")
