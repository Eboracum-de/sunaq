from __future__ import annotations

import json
from types import SimpleNamespace

from starlette.requests import Request

import rag.api as api
import rag.search as search
import rag.acl as acl_module
from rag.acl import AclDecision, NextcloudLiveAcl
from rag.vector import VectorStore


def _request(headers: dict[str, str]) -> Request:
    raw = [
        (str(key).lower().encode("latin1"), str(value).encode("latin1"))
        for key, value in headers.items()
    ]
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/search",
        "headers": raw,
    })


def test_es_acl_prefilter_uses_owner_users_groups_but_not_circles():
    bool_query = {"must": [], "should": [], "must_not": [], "filter": []}
    applied = search._apply_acl_prefilter_to_es(
        bool_query,
        user="alice",
        groups=["finance", "board"],
    )
    assert applied is True
    rendered = json.dumps(bool_query, ensure_ascii=False)
    assert '"owner"' in rendered
    assert '"users"' in rendered
    assert '"groups"' in rendered
    assert "alice" in rendered
    assert "finance" in rendered
    assert "board" in rendered
    assert "circles" not in rendered


def test_es_acl_prefilter_fails_open_when_group_context_is_missing():
    bool_query = {"must": [], "should": [], "must_not": [], "filter": []}
    assert search._apply_acl_prefilter_to_es(
        bool_query,
        user="alice",
        groups=None,
    ) is False
    assert bool_query["filter"] == []


def test_live_acl_prefilter_identity_uses_ocs_current_user(monkeypatch):
    cfg = {
        "nextcloud": {"base_url": "https://cloud.example.test"},
        "acl": {
            "enabled": True,
            "identity_mode": "single_user",
            "username_env": "TEST_NC_USER",
            "password_env": "TEST_NC_PASSWORD",
        },
    }
    monkeypatch.setenv("TEST_NC_USER", "login-name")
    monkeypatch.setenv("TEST_NC_PASSWORD", "app-secret")
    live_acl = NextcloudLiveAcl(cfg)
    seen = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "ocs": {
                    "meta": {"status": "ok", "statuscode": 100},
                    "data": {
                        "id": "internal-uid",
                        "groups": ["finance", "board", "finance"],
                    },
                }
            }

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return Response()

    monkeypatch.setattr(acl_module.httpx, "get", fake_get)
    uid, groups = live_acl.prefilter_identity()
    assert uid == "internal-uid"
    assert groups == ["finance", "board"]
    assert seen["url"] == "https://cloud.example.test/ocs/v1.php/cloud/user"
    assert seen["params"] == {"format": "json"}
    assert seen["auth"] == ("login-name", "app-secret")
    assert seen["headers"]["OCS-APIRequest"] == "true"


def test_live_acl_prefilter_identity_accepts_user_without_groups(monkeypatch):
    cfg = {
        "nextcloud": {"base_url": "https://cloud.example.test"},
        "acl": {
            "enabled": True,
            "identity_mode": "single_user",
            "username_env": "TEST_NC_USER",
            "password_env": "TEST_NC_PASSWORD",
        },
    }
    monkeypatch.setenv("TEST_NC_USER", "alice")
    monkeypatch.setenv("TEST_NC_PASSWORD", "secret")
    live_acl = NextcloudLiveAcl(cfg)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "ocs": {
                    "meta": {"status": "ok", "statuscode": 100},
                    "data": {"id": "alice", "groups": []},
                }
            }

    monkeypatch.setattr(acl_module.httpx, "get", lambda *args, **kwargs: Response())
    assert live_acl.prefilter_identity() == ("alice", [])


def test_api_prefilter_uses_server_resolved_identity_without_group_header(monkeypatch):
    prefilter = api.app_config.setdefault("acl", {}).setdefault("prefilter", {})
    monkeypatch.setitem(prefilter, "enabled", True)
    monkeypatch.setattr(api.live_acl, "enabled", True)
    monkeypatch.setattr(api.live_acl, "identity_mode", "credential_store")
    seen = {}

    def resolve(user_id):
        seen["user_id"] = user_id
        return "internal-alice", ["finance", "board"]

    monkeypatch.setattr(api.live_acl, "prefilter_identity", resolve)

    user, groups = api._acl_prefilter_context(_request({
        "X-RAG-User-ID": "frontend-a::alice",
    }))
    assert seen["user_id"] == "frontend-a::alice"
    assert user == "internal-alice"
    assert groups == ["finance", "board"]


def test_api_prefilter_ignores_client_supplied_group_header(monkeypatch):
    prefilter = api.app_config.setdefault("acl", {}).setdefault("prefilter", {})
    monkeypatch.setitem(prefilter, "enabled", True)
    monkeypatch.setattr(api.live_acl, "enabled", True)
    monkeypatch.setattr(api.live_acl, "identity_mode", "credential_store")
    monkeypatch.setattr(
        api.live_acl,
        "prefilter_identity",
        lambda user_id: ("internal-alice", ["server-group"]),
    )

    user, groups = api._acl_prefilter_context(_request({
        "X-RAG-User-ID": "frontend-a::alice",
        "X-RAG-User-Groups": '["spoofed-group"]',
    }))
    assert user == "internal-alice"
    assert groups == ["server-group"]


def _fake_unspecific_search_result():
    return {
        "plan": None,
        "entity_resolution": {},
        "search_spec": {},
        "retrieval_mode": "rrf_no_reranker",
        "retrieval_strategy": "forced_fusion",
        "retrieval_message": "",
        "retrieval_arms": ["files"],
        "source_scopes": None,
        "raw_results": False,
        "force_unspecific": True,
        "retrieval_signal": {
            "decision": {
                "strategy": "unspecific",
                "applied_strategy": "forced_fusion",
            }
        },
        "lookup_filename": None,
        "reranker_used": False,
        "reranker_error": None,
        "timings": {},
        "statistics": {"returned": 1},
        "orientation_candidates": [],
        "results": [
            {
                "document_id": "files:100",
                "title": "Musterweg 280",
                "context_text": "Treffer",
            }
        ],
    }


def test_unspecific_feedback_is_hidden_when_acl_authorizes_zero_candidates(monkeypatch):
    monkeypatch.setattr(api.graph_queue, "mark_activity", lambda *_: None)
    monkeypatch.setattr(api.live_acl, "enabled", True)
    monkeypatch.setattr(api.live_acl, "identity_mode", "single_user")
    monkeypatch.setattr(api, "_acl_prefilter_context", lambda request: (None, None))

    def run_search(**kwargs):
        assert kwargs["force_unspecific"] is True
        return _fake_unspecific_search_result()

    monkeypatch.setattr(api, "perform_search", run_search)
    monkeypatch.setattr(
        api.live_acl,
        "authorize",
        lambda results, rag_user_id=None: AclDecision(True, [], len(results), 0),
    )

    payload = api.search(
        api.SearchRequest(query="Suche Informationen über die Musterweg 280"),
        _request({"X-RAG-User-ID": "user-2"}),
    )
    assert payload["results"] == []
    assert payload["retrieval_mode"] == "no_results"
    assert payload["retrieval_message"] == ""


def test_unspecific_feedback_survives_only_with_acl_visible_candidate(monkeypatch):
    monkeypatch.setattr(api.graph_queue, "mark_activity", lambda *_: None)
    monkeypatch.setattr(api.live_acl, "enabled", True)
    monkeypatch.setattr(api.live_acl, "identity_mode", "single_user")
    monkeypatch.setattr(api, "_acl_prefilter_context", lambda request: (None, None))
    monkeypatch.setattr(api, "perform_search", lambda **kwargs: _fake_unspecific_search_result())

    visible = _fake_unspecific_search_result()["results"][0]
    monkeypatch.setattr(
        api.live_acl,
        "authorize",
        lambda results, rag_user_id=None: AclDecision(True, [visible], len(results), 1),
    )

    payload = api.search(
        api.SearchRequest(query="Suche Informationen über die Musterweg 280"),
        _request({"X-RAG-User-ID": "user-1"}),
    )
    assert payload["results"] == []
    assert payload["retrieval_mode"] == "unspecific"
    assert "unspezifisches Trefferbild" in payload["retrieval_message"]


class _FakeQdrantClient:
    def __init__(self):
        self.filter = None

    def query_points(self, **kwargs):
        self.filter = kwargs["query_filter"]
        return SimpleNamespace(points=[])


def test_qdrant_prefilter_keeps_source_scope_and_acl_as_separate_must_filters():
    store = object.__new__(VectorStore)
    store.collection = "test"
    store.client = _FakeQdrantClient()

    store.search(
        [0.1, 0.2],
        limit=5,
        include_source_origins=["document"],
        acl_user="alice",
        acl_groups=["finance"],
    )

    payload = store.client.filter.model_dump(exclude_none=True)
    rendered = json.dumps(payload, ensure_ascii=False)
    # Source-origin OR and ACL-identity OR must both be satisfied.
    assert len(payload["must"]) == 2
    assert "source_origin" in rendered
    assert "owner" in rendered
    assert "users" in rendered
    assert "groups" in rendered
    assert "circles" not in rendered
