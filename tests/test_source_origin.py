from rag.source_origin import classify_source_origin, is_internal_excluded_path, path_is_under


def test_web_archive_path_is_classified_separately():
    assert classify_source_origin("Webarchiv/2026-08/29-194712-a83f/recherche.md") == "web_archive"
    assert classify_source_origin("Nordstern/Vertrag.pdf") == "internal"


def test_path_under_root_is_component_aware_and_case_insensitive():
    assert path_is_under("/Webarchiv/2026-08/test.txt", "Webarchiv")
    assert path_is_under("webarchiv", "Webarchiv")
    assert not path_is_under("Webarchive-Anders/test.txt", "Webarchiv")


def test_hidden_mail_metadata_is_machine_metadata_and_excluded():
    assert classify_source_origin("Mailarchiv/x/.mailmeta.json") == "machine_metadata"
    assert classify_source_origin("Mailarchiv/x/.20260830-101500_42.mailmeta.json") == "machine_metadata"
    assert is_internal_excluded_path("Mailarchiv/x/.mailmeta.json")


def test_any_hidden_file_basename_is_machine_metadata_and_excluded():
    assert classify_source_origin("Documents/.conversation.json") == "machine_metadata"
    assert classify_source_origin("Documents/.notes.md") == "machine_metadata"
    assert is_internal_excluded_path("Documents/.conversation.json")
    # Only the basename is treated as hidden; a normal file below a dot-directory
    # keeps its ordinary source classification.
    assert classify_source_origin("Documents/.private/notes.md") == "internal"


def test_per_user_web_archive_root_is_excluded(tmp_path, monkeypatch):
    import sqlite3
    import rag.source_origin as source_origin

    db = tmp_path / "users.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE user_web_settings(canonical_user_id TEXT, enabled INTEGER, archive_enabled INTEGER, target_path TEXT)")
    con.execute("INSERT INTO user_web_settings VALUES('u1',1,1,'Research/Alice-Web')")
    con.commit()
    con.close()

    monkeypatch.setattr(source_origin, "_credential_store_path", lambda: db)
    source_origin._user_archive_roots_for_stamp.cache_clear()
    assert source_origin.is_internal_excluded_path("Research/Alice-Web/2026-09/run/recherche.md")
    assert source_origin.classify_source_origin("Research/Alice-Web/x.txt") == "web_archive"
    assert not source_origin.is_internal_excluded_path("Research/Alice/x.txt")



def test_per_user_chat_archive_root_is_classified_and_excluded(tmp_path, monkeypatch):
    import sqlite3
    import rag.source_origin as source_origin

    db = tmp_path / "users.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE user_chat_settings(canonical_user_id TEXT, target_path TEXT)")
    con.execute("INSERT INTO user_chat_settings VALUES('u1','Research/Alice-Chats')")
    con.commit()
    con.close()

    monkeypatch.setattr(source_origin, "_credential_store_path", lambda: db)
    source_origin._chat_archive_roots_for_stamp.cache_clear()
    assert source_origin.is_internal_excluded_path("Research/Alice-Chats/2026-09/chat.md")
    assert source_origin.classify_source_origin("Research/Alice-Chats/chat.md") == "chat_archive"
    assert not source_origin.is_internal_excluded_path("Research/Alice/x.txt")


def test_sunaq_chat_archives_and_legacy_aki_archives_are_both_classified():
    assert classify_source_origin("SunaQ-Chats/2026-09/Test.md") == "chat_archive"
    assert classify_source_origin("AKI-Chats/2026-09/Legacy.md") == "chat_archive"
    assert classify_source_origin("SunaQ-Chats/.deadbeef.sunaq.json") == "machine_metadata"
    assert classify_source_origin("AKI-Chats/.deadbeef.akirag.json") == "machine_metadata"


def test_elasticsearch_queries_exclude_sunaq_sidecars_before_candidate_limit():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    source = (root / "rag" / "search.py").read_text(encoding="utf-8")
    assert source.count('{"wildcard": {"title.keyword": "*/.*.sunaq.json"}}') >= 2

def test_legacy_web_archive_origin_is_retrieved_via_chatarchive_scope():
    import rag.source_origin as source_origin

    record = {"source_origin": "web_archive", "path": "Webarchiv/legacy/source.txt"}
    assert source_origin.source_scope_allows_record("files:legacy", record["path"], {"chatarchive"}, indexed_origin=record["source_origin"])
    assert not source_origin.source_scope_allows_record("files:legacy", record["path"], {"documents"}, indexed_origin=record["source_origin"])

