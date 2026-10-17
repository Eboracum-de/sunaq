import sqlite3

from rag.openai_provider import _parse_retrieval_directives


def test_source_directives_are_orthogonal_to_retrieval_arms():
    parsed = _parse_retrieval_directives('/documents /mailarchive /vector Rechnung 2025')
    assert parsed.error is None
    assert parsed.source_scopes == {'documents', 'mailarchive'}
    assert parsed.retrieval_arms == {'vector'}
    assert parsed.web_requested is False
    assert parsed.query == 'Rechnung 2025'


def test_web_alone_is_web_only_but_documents_plus_web_is_mixed():
    web = _parse_retrieval_directives('/web aktueller Stand')
    assert web.web_requested is True
    assert web.web_only is True
    mixed = _parse_retrieval_directives('/documents /web aktueller Stand')
    assert mixed.web_requested is True
    assert mixed.web_only is False
    assert mixed.source_scopes == {'documents'}


def test_chat_archive_evidence_is_admin_gated_by_default():
    root = __import__("pathlib").Path(__file__).resolve().parent.parent
    cfg = __import__("yaml").safe_load((root / "config.yaml").read_text())
    assert cfg["chat_archive"]["enabled"] is False

    provider_source = (root / "rag" / "openai_provider.py").read_text()
    api_source = (root / "rag" / "api.py").read_text()
    assert 'os.getenv("CHAT_ARCHIVE_ENABLED", _chat_archive_default)' in provider_source
    assert 'def _disabled_requested_sources(' in provider_source
    assert '"chatarchive": bool(chat_enabled' in provider_source
    assert 'model_caps.get("chatarchive", True)' in provider_source
    assert 'classify_source_origin(' in api_source
    assert '== "chat_archive"' in api_source
    assert 'not CHAT_ARCHIVE_ENABLED' in api_source
    assert '"chat_archive_enabled": chat_enabled' in provider_source
    assert '"reason": "chat_archive_disabled"' in provider_source


def test_legacy_webarchive_directive_aliases_to_chatarchive():
    parsed = _parse_retrieval_directives('/webarchive /chatarchive Vorgang')
    assert parsed.error is None
    assert parsed.source_scopes == {'chatarchive'}
    assert parsed.retrieval_arms is None


def test_source_origin_classifies_mail_and_chat_scopes(tmp_path, monkeypatch):
    import rag.source_origin as source_origin

    db = tmp_path / 'users.sqlite'
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE mail_accounts(target_path TEXT, eml_target_path TEXT)')
    con.execute("INSERT INTO mail_accounts VALUES('Archive/Mail','Archive/EML')")
    con.commit()
    con.close()

    monkeypatch.setattr(source_origin, '_credential_store_path', lambda: db)
    source_origin._mail_archive_roots_for_stamp.cache_clear()
    assert source_origin.classify_source_origin('Archive/Mail/Inbox/x.html') == 'mail_archive'
    assert source_origin.classify_source_origin('AKI-Chats/abc.html') == 'chat_archive'
    assert source_origin.source_scope_allows_path('Ordner/Datei.pdf', {'documents'})
    assert not source_origin.source_scope_allows_path('Archive/Mail/Inbox/x.html', {'documents'})
    assert source_origin.source_scope_allows_path('Archive/Mail/Inbox/x.html', {'mailarchive'})
    assert source_origin.source_scope_allows_path('AKI-Chats/abc.html', {'chatarchive'})
    assert source_origin.source_scope_allows_path('Ordner/Datei.pdf', None)
    assert not source_origin.source_scope_allows_path('Archive/Mail/Inbox/x.html', None)
    assert not source_origin.source_scope_allows_path('AKI-Chats/abc.html', None)


def test_chat_archive_root_history_remains_excluded_from_implicit_documents(tmp_path, monkeypatch):
    import rag.source_origin as source_origin

    db = tmp_path / "users.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE user_chat_settings(canonical_user_id TEXT, target_path TEXT, updated_at REAL)"
    )
    con.execute(
        "CREATE TABLE chat_archive_roots(target_path TEXT PRIMARY KEY, first_seen_at REAL, last_seen_at REAL)"
    )
    con.execute("INSERT INTO user_chat_settings VALUES('u1','SunaQ-Chats',1)")
    con.execute("INSERT INTO chat_archive_roots VALUES('Research/Old-Chats',1,2)")
    con.commit()
    con.close()

    monkeypatch.setattr(source_origin, "_credential_store_path", lambda: db)
    source_origin._chat_archive_roots_for_stamp.cache_clear()
    assert source_origin.classify_source_origin("Research/Old-Chats/old.md") == "chat_archive"
    assert not source_origin.source_scope_allows_path("Research/Old-Chats/old.md", None)
    assert source_origin.source_scope_allows_path(
        "Research/Old-Chats/old.md", {"chatarchive"}
    )


def test_sunaq_client_has_scope_ui_and_server_side_chat_archive():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / 'clients' / 'nextcloud' / 'sunaq'
    main = (root / 'templates' / 'main.php').read_text(encoding='utf-8')
    proxy = (root / 'lib' / 'Service' / 'RagProxy.php').read_text(encoding='utf-8')
    store = (root / 'lib' / 'Service' / 'ChatStore.php').read_text(encoding='utf-8')
    controller = (root / 'lib' / 'Controller' / 'ChatController.php').read_text(encoding='utf-8')
    for scope in ['documents', 'mailarchive', 'chatarchive', 'web']:
        assert f'value="{scope}"' in main
    assert 'value="webarchive"' not in main
    assert 'value="documents" checked' in main
    assert 'value="mailarchive" checked' not in main
    assert 'explicit user source selection wins over UI state' in proxy
    assert "const FOLDER = 'SunaQ-Chats'" in store
    assert "chat_archive_path" in store
    assert "LEGACY_FOLDER" not in store
    assert "$record['archive_path'] = $record['dossier_path'] . '/' . $chatFile;" in store
    assert "LEGACY_DOSSIER_CHAT_FILE = 'chat.md'" in store
    assert '.akirag.json' in store
    assert "'source_origin' => 'chat_archive'" in store
    assert "'format' => 'markdown'" in store
    assert "'.md'" in store
    assert "renderMarkdown" in store
    assert "renderHtml" not in store
    assert "/v1/archive/chat/register" in proxy
    assert "registerChatArchive" in controller


def test_chat_archive_registration_writes_registry_before_es_mirror(monkeypatch):
    from types import SimpleNamespace

    import rag.api as api
    class FakeAcl:
        enabled = True

        def resolve_visible_file_path(self, document_id, *, rag_user_id=None):
            assert rag_user_id == 'alice'
            assert document_id == 'files:74710'
            return 'AKI-Chats/2026-09-22 - Musterweg 280 - abcdef12.md'

    calls = []
    monkeypatch.setattr(api, 'live_acl', FakeAcl())
    monkeypatch.setattr(api, 'chat_archive_roots', lambda: ('AKI-Chats',))
    monkeypatch.setattr(
        api,
        'register_document',
        lambda document_id, source_origin, **kwargs: calls.append(
            (document_id, source_origin, kwargs)
        ) or True,
    )
    monkeypatch.setattr(
        api,
        'auto_mirror_registry_to_elasticsearch',
        lambda **kwargs: {'checked': 1, 'updated': 0, 'missing': 1},
    )

    result = api.register_chat_archive_source(
        api.ChatArchiveRegisterRequest(
            document_id='files:74710',
            path='AKI-Chats/2026-09-22 - Musterweg 280 - abcdef12.md',
        ),
        SimpleNamespace(headers={'x-rag-user-id': 'alice'}),
    )

    assert result['ok'] is True
    assert result['source_origin'] == 'chat_archive'
    assert calls == [(
        'files:74710',
        'chat_archive',
        {
            'source_path': 'AKI-Chats/2026-09-22 - Musterweg 280 - abcdef12.md',
            'classification_source': 'chat_archive_write',
        },
    )]


def test_vector_default_matches_explicit_documents_only(monkeypatch):
    from types import SimpleNamespace
    import rag.search as search

    calls = []

    class FakeEmbeddings:
        kind = 'fake'
        model = 'fake'
        profile = 'plain'

        def embed_query(self, _query):
            return [0.0, 1.0]

    class FakeStore:
        def search(self, _vector, **kwargs):
            calls.append(kwargs)
            return []

    monkeypatch.setattr(search, 'embeddings', FakeEmbeddings())
    monkeypatch.setattr(search, 'store', FakeStore())

    plan = SimpleNamespace(semantic_query='Musterweg')
    search.vector_search(plan, {}, source_scopes=None)
    search.vector_search(plan, {}, source_scopes={'documents'})

    expected = ['mail_archive', 'web_archive', 'chat_archive']
    assert calls[0]['exclude_source_origins'] == expected
    assert calls[0]['include_source_origins'] is None
    assert calls[1]['exclude_source_origins'] == expected
    assert calls[1]['include_source_origins'] is None


def test_vector_documents_scope_excludes_all_archive_origins(monkeypatch):
    from types import SimpleNamespace
    import rag.search as search

    calls = []

    class FakeEmbeddings:
        kind = 'fake'
        model = 'fake'
        profile = 'plain'

        def embed_query(self, _query):
            return [0.0, 1.0]

    class FakeStore:
        def search(self, _vector, **kwargs):
            calls.append(kwargs)
            return []

    monkeypatch.setattr(search, 'embeddings', FakeEmbeddings())
    monkeypatch.setattr(search, 'store', FakeStore())

    search.vector_search(
        SimpleNamespace(semantic_query='Musterweg'),
        {},
        source_scopes={'documents'},
    )

    assert set(calls[0]['exclude_source_origins']) == {
        'mail_archive',
        'web_archive',
        'chat_archive',
    }
    assert calls[0]['include_source_origins'] is None


def test_use_findings_enrichment_is_deferred_until_after_answer_generation():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    provider = (root / "rag" / "openai_provider.py").read_text(encoding="utf-8")
    assert "deferred_use_findings: dict[str, Any] | None = None" in provider
    assert "_schedule_use_research_findings(**deferred_use_findings)" in provider
    use_start = provider.index("if use_references or implicit_filename_use:")
    use_end = provider.index("\n        if elastic_mode:", use_start)
    assert use_start >= 0 and use_end > use_start
    use_branch = provider[use_start:use_end]
    assert "await _store_use_research_findings(" not in use_branch
    assert "_BACKGROUND_TASKS.add(task)" in provider
    assert '_ACTIVE_PROGRESS_ID.set("")' in provider

def test_chatarchive_scope_includes_legacy_web_archive_provenance():
    import rag.source_origin as source_origin

    assert source_origin.normalize_source_scopes({"webarchive"}) == {"chatarchive"}
    assert source_origin.source_origins_for_scopes({"chatarchive"}) == {
        "chat_archive",
        "web_archive",
    }

