from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "install" / "sync-container-secrets.sh"


def test_service_secret_materializer_handles_quotes_empty_override_and_rotation(tmp_path):
    prefix = tmp_path / "sunaq"
    runtime = prefix / "runtime"
    runtime.mkdir(parents=True)
    provider = prefix / "provider.env"
    runtime_env = prefix / "runtime.env"

    provider.write_text(
        "LLM_BASE_URL=http://127.0.0.1:11434\n"
        "LLM_API_KEY='provider-old'\n"
        "RAG_PROVIDER_INTERNAL_KEY='provider-machine'\n",
        encoding="utf-8",
    )
    runtime_env.write_text(
        "RAG_INTERNAL_API_KEY='internal-machine'\n"
        "RAG_PROVIDER_INTERNAL_KEY=\"provider-runtime\"\n"
        "RAG_ADMIN_PASSWORD='admin pass'\n"
        "LLM_API_KEY=\n",
        encoding="utf-8",
    )

    stale = runtime / "service-secrets"
    stale.mkdir()
    (stale / "STALE_API_KEY").write_text("stale", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(SCRIPT), str(prefix)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    secrets = runtime / "service-secrets"
    assert (secrets / "RAG_INTERNAL_API_KEY").read_text() == "internal-machine"
    assert (secrets / "RAG_PROVIDER_INTERNAL_KEY").read_text() == "provider-runtime"
    assert (secrets / "RAG_ADMIN_PASSWORD").read_text() == "admin pass"
    # An explicitly empty runtime value overrides the older provider.env value.
    assert (secrets / "LLM_API_KEY").read_text() == ""
    assert not (secrets / "STALE_API_KEY").exists()

    container_env = (prefix / "runtime.container.env").read_text()
    assert "RAG_PROVIDER_INTERNAL_KEY_FILE=/run/sunaq-secrets/RAG_PROVIDER_INTERNAL_KEY" in container_env
    assert "RAG_INTERNAL_API_KEY_FILE=/run/sunaq-secrets/RAG_INTERNAL_API_KEY" in container_env
    assert "LLM_API_KEY_FILE=/run/sunaq-secrets/LLM_API_KEY" in container_env
    assert "provider-runtime" not in container_env
    assert "internal-machine" not in container_env
    assert "provider-old" not in container_env
    assert "LLM_BASE_URL=http://127.0.0.1:11434" in container_env

    service_env = (prefix / "runtime.service.env").read_text()
    assert "RAG_PROVIDER_INTERNAL_KEY_FILE=" in service_env
    assert "RAG_ADMIN_PASSWORD_FILE=" in service_env
    assert "admin pass" not in service_env
