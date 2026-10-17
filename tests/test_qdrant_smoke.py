import json
import sys

import rag.qdrant_smoke as smoke


def test_qdrant_smoke_main_reports_embedding_offline_without_traceback(monkeypatch, tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("{}\n", encoding="utf-8")

    def fail_probe(*args, **kwargs):
        raise smoke.SmokeProbeError(
            "embedding",
            "offline",
            "ollama embedding endpoint not reachable (http://127.0.0.1:11434)",
        )

    monkeypatch.setattr(smoke, "run_probe", fail_probe)
    monkeypatch.setattr(sys, "argv", ["qdrant_smoke", "--config", str(config)])

    assert smoke.main() == 20
    output = capsys.readouterr()
    assert "SMOKE_FAIL component=embedding reason=offline" in output.out
    assert "Traceback" not in output.out
    assert "Traceback" not in output.err


def test_qdrant_smoke_json_error_is_machine_readable(monkeypatch, tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(
        smoke,
        "run_probe",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            smoke.SmokeProbeError("embedding", "error", "model missing")
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["qdrant_smoke", "--config", str(config), "--json"],
    )

    assert smoke.main() == 21
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "status": "down",
        "component": "embedding",
        "reason": "error",
        "error": "model missing",
    }
