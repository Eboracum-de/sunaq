from pathlib import Path


def _requirements(name: str) -> set[str]:
    lines = Path(name).read_text(encoding="utf-8").splitlines()
    return {line.split(";", 1)[0].strip().lower() for line in lines if line.strip() and not line.lstrip().startswith("#")}


def test_admin_ui_template_dependency_is_explicit_in_all_runtime_profiles():
    for filename in ("requirements.txt", "requirements-dockerized.txt"):
        requirements = _requirements(filename)
        assert any(item.startswith("jinja2") for item in requirements), filename
