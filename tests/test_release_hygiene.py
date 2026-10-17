from pathlib import Path
import os
import subprocess

from rag.version import VERSION


def test_public_release_identity_is_consistent() -> None:
    assert VERSION == "0.8.7-rc1"

    root = Path(".")
    assert f"release: {VERSION}" in (root / "versions.lock.yaml").read_text(encoding="utf-8")
    assert f"SunaQ / Eboracum Research Gateway - {VERSION}" in (
        root / "config.yaml"
    ).read_text(encoding="utf-8")
    assert f"## {VERSION} " in (root / "CHANGELOG.md").read_text(encoding="utf-8")

    notes = root / f"RELEASE-NOTES-{VERSION}.md"
    assert notes.is_file()
    assert f"# SunaQ {VERSION}" in notes.read_text(encoding="utf-8")
    assert f"`RELEASE-NOTES-{VERSION}.md`" in (
        root / "README.md"
    ).read_text(encoding="utf-8")


def test_public_tree_has_no_committed_runtime_or_secret_material() -> None:
    """Inspect only Git-tracked paths; test/runtime files created locally are irrelevant."""
    forbidden_names = {
        "runtime.env",
        "provider.env",
        "state.sqlite",
        "research.sqlite",
        "graph_queue.sqlite",
        "mail_state.sqlite",
    }
    forbidden_suffixes = {".pem", ".key", ".p12", ".pfx", ".sqlite-wal", ".sqlite-shm"}

    raw = subprocess.check_output(["git", "ls-files", "-z"])
    tracked = [Path(os.fsdecode(item)) for item in raw.split(b"\0") if item]
    offenders = [
        str(path)
        for path in tracked
        if path.name in forbidden_names
        or any(path.name.endswith(suffix) for suffix in forbidden_suffixes)
    ]

    assert offenders == [], "runtime/secret material leaked into public tree:\n" + "\n".join(offenders)
