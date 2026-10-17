from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_admin_web_settings_use_chat_archive_lifecycle():
    source = (ROOT / "rag" / "admin_ui.py").read_text(encoding="utf-8")
    template = (ROOT / "rag" / "templates" / "admin" / "user.html").read_text(
        encoding="utf-8"
    )

    assert "web_snapshot_engine_enabled = bool(" in source
    assert "archive_requested" not in source
    assert "target_path = data.get(\"target_path\"" not in source
    assert "user_store.set_web_settings(" in source

    assert "Chat/Research-Run" in template
    assert "separaten Webarchiv-Zielpfad gibt es nicht mehr" in template
    web_section = template[
        template.index("<h2>Web Research</h2>"):
        template.index("</section>", template.index("<h2>Web Research</h2>"))
    ]
    assert 'name="archive_enabled"' not in web_section
    assert 'name="target_path"' not in web_section


def test_nextcloud_app_version_tracks_chatarchive_taxonomy_change():
    info = (
        ROOT / "clients" / "nextcloud" / "sunaq" / "appinfo" / "info.xml"
    ).read_text(encoding="utf-8")
    assert "<version>0.3.16</version>" in info


def test_admin_uses_central_architecture_tier_compatibility_policy():
    source = (ROOT / "rag" / "admin_ui.py").read_text(encoding="utf-8")
    assert "architecture_tier as effective_architecture_tier" in source
    assert "architecture_tier = effective_architecture_tier(cfg)" in source
    assert 'cfg_get(cfg, "architecture.tier", default="src")' not in source
