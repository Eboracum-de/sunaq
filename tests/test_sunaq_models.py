from pathlib import Path

import pytest
import yaml

from rag.sunaq_models import load_model_registry


def _write_profile(root: Path, directory: str, payload: dict, prompts: dict[str, str] | None = None):
    model_dir = root / directory
    (model_dir / "prompts").mkdir(parents=True)
    (model_dir / "profile.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    for name, content in (prompts or {}).items():
        (model_dir / "prompts" / name).write_text(content, encoding="utf-8")


def test_sunaq_models_load_once_with_config_and_prompt_overrides(tmp_path):
    models = tmp_path / "models"
    _write_profile(
        models,
        "standard",
        {
            "id": "sunaq-standard",
            "name": "Standard",
            "default": True,
            "aliases": ["nextcloud-hybrid-rag"],
        },
    )
    _write_profile(
        models,
        "chef",
        {
            "id": "chef",
            "name": "Chef",
            "search": {"final_limit": 23},
            "retrieval_planner": {"max_retrieval_rounds": 2},
            "roles": {"answer": {"model": "strong-answer"}},
            "prompts": {"answer": "answer.txt"},
        },
        {"answer.txt": "CHEF ANSWER PROMPT"},
    )
    base = {
        "search": {"final_limit": 15, "es_limit": 50},
        "retrieval_planner": {"max_retrieval_rounds": 1, "max_tokens": 700},
        "graph_queue": {"worker": {"enabled": False}},
    }

    registry = load_model_registry(base, models_dir=models)

    assert registry.default_model_id == "sunaq-standard"
    assert registry.get("nextcloud-hybrid-rag").model_id == "sunaq-standard"
    chef = registry.get("chef")
    assert chef.section("search") == {"final_limit": 23}
    assert chef.section("retrieval_planner") == {"max_retrieval_rounds": 2}
    assert "es_limit" not in chef.section("search")
    assert "max_tokens" not in chef.section("retrieval_planner")
    assert chef.roles["answer"]["model"] == "strong-answer"
    assert chef.prompts["answer"] == "CHEF ANSWER PROMPT"
    # Background worker policy is inherited but cannot be overridden by a profile.
    assert chef.config["graph_queue"]["worker"]["enabled"] is False




def test_sunaq_model_prompt_cannot_escape_model_directory(tmp_path):
    models = tmp_path / "models"
    model = models / "bad"
    model.mkdir(parents=True)
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    (model / "profile.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "bad",
                "default": True,
                "prompts": {"answer": "../../secret.txt"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="escapes model directory"):
        load_model_registry({}, models_dir=models)


def test_sunaq_models_reject_plaintext_role_api_keys(tmp_path, monkeypatch):
    # The registry intentionally preserves role dictionaries; plaintext secret
    # rejection happens while constructing the role backends.
    from rag.llm_roles import build_role_backends

    with pytest.raises(RuntimeError, match="api_key_env"):
        build_role_backends(
            default_backend="openai",
            default_base_url="https://example.test/v1",
            default_model="base",
            default_api_key="",
            default_verify_tls=True,
            default_ca_file=None,
            role_overrides={"answer": {"api_key": "secret"}},
        )


def test_sunaq_role_override_can_change_model_without_mutating_environment(monkeypatch):
    from rag.llm_roles import build_role_backends

    monkeypatch.setenv("LLM_BACKEND", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("ANSWER_LLM_MODEL", "environment-answer")
    built = build_role_backends(
        default_backend="openai",
        default_base_url="https://example.test/v1",
        default_model="base",
        default_api_key="",
        default_verify_tls=True,
        default_ca_file=None,
        role_overrides={"answer": {"model": "chef-answer"}},
    )
    assert built["answer"].model == "chef-answer"
    assert built["planner"].model == "base"


def test_legacy_provider_model_id_remains_an_alias_for_default(tmp_path, monkeypatch):
    models = tmp_path / "models"
    _write_profile(
        models,
        "standard",
        {"id": "sunaq-standard", "name": "Standard", "default": True},
    )
    monkeypatch.setenv("PROVIDER_MODEL_ID", "legacy-custom-rag")
    registry = load_model_registry({}, models_dir=models)
    assert registry.get("legacy-custom-rag").model_id == "sunaq-standard"


def test_search_runtime_config_is_context_local_and_restored():
    from rag import search

    global_es_limit = search._es_limit()
    global_graph_enabled = search._graph_retrieval_enabled()

    with search.use_runtime_model_config(
        {
            "search": {"es_limit": 7},
            "graph_retrieval": {"enabled": False},
        }
    ):
        assert search._es_limit() == 7
        assert search._graph_retrieval_enabled() is False

    assert search._es_limit() == global_es_limit
    assert search._graph_retrieval_enabled() == global_graph_enabled


def test_graph_search_honors_runtime_profile_disable(monkeypatch):
    from rag import search

    monkeypatch.setattr(search, "NEO4J_ENABLED", True)
    diagnostics = {}
    with search.use_runtime_model_config({"graph_retrieval": {"enabled": False}}):
        assert search.graph_search(
            {"entities": [{"status": "resolved", "entity_id": "entity-1"}]},
            diagnostics=diagnostics,
        ) == []
    assert diagnostics["enabled"] is False
    assert diagnostics["mode"] == "disabled"


def test_reranker_pool_default_is_resolved_per_runtime_profile():
    from rag import search

    fused = [
        {"document_id": f"files:{idx}", "es_rank": idx, "score": 1.0 / idx}
        for idx in range(1, 6)
    ]
    with search.use_runtime_model_config({"search": {"rerank_candidates": 2}}):
        selected = search.deduplicate_for_reranker(fused)
    assert len(selected) == 2


def test_profile_reranker_override_does_not_raise_nameerror():
    from rag import reranker as reranker_module

    with pytest.raises(RuntimeError, match="deaktiviert"):
        reranker_module.rerank_results(
            "test",
            [{"document_id": "files:1", "title": "Test", "content": "Text"}],
            config_override={"backend": "none"},
        )


def test_unknown_model_http_error_is_not_wrapped_as_500(monkeypatch):
    from fastapi import HTTPException
    import rag.api as api

    def reject_model(_model):
        raise HTTPException(status_code=400, detail="unknown model")

    monkeypatch.setattr(api, "_resolve_sunaq_model", reject_model)
    request = api.PlanRequest(query="test", model="missing-model")

    for endpoint in (api.query_context, api.plan):
        with pytest.raises(HTTPException) as exc:
            endpoint(request)
        assert exc.value.status_code == 400



def test_rag_admin_exposes_per_user_sunaq_model_permissions():
    root = Path(__file__).resolve().parent.parent
    admin = (root / "rag" / "admin_ui.py").read_text(encoding="utf-8")
    user_template = (root / "rag" / "templates" / "admin" / "user.html").read_text(
        encoding="utf-8"
    )
    store = (root / "rag" / "credential_store.py").read_text(encoding="utf-8")

    assert 'CREATE TABLE IF NOT EXISTS user_model_settings' in store
    assert "def get_model_settings(" in store
    assert "def set_model_settings(" in store
    assert '@router.post("/users/{canonical_user_id}/models"' in admin
    assert "user_store.set_model_settings(" in admin
    assert "model_registry.canonical_id(" in admin

    assert "<h2>SunaQ Modelle</h2>" in user_template
    assert 'name="allowed_model_id"' in user_template
    assert 'name="default_model_id"' in user_template
    assert "admin_user_models" in user_template



def test_shipped_sunaq_profiles_own_request_local_settings_and_core_prompts():
    root = Path(__file__).resolve().parent.parent
    global_config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    request_sections = {
        "search",
        "retrieval_planner",
        "evidence_control",
        "retrieval_signal",
        "entity_resolution",
        "graph_retrieval",
        "reranker",
        "context_enrichment",
        "answer_context",
    }
    assert request_sections.isdisjoint(global_config)

    standard = yaml.safe_load(
        (root / "models" / "standard" / "profile.yaml").read_text(encoding="utf-8")
    )
    thorough = yaml.safe_load(
        (root / "models" / "thorough" / "profile.yaml").read_text(encoding="utf-8")
    )
    deep = yaml.safe_load(
        (root / "models" / "deep" / "profile.yaml").read_text(encoding="utf-8")
    )
    assert request_sections.issubset(standard)
    assert request_sections.issubset(thorough)
    assert request_sections.issubset(deep)

    assert standard["search"]["es_limit"] == 50
    assert standard["search"]["vector_limit"] == 80
    assert standard["search"]["rerank_candidates"] == 10
    assert standard["search"]["final_limit"] == 10
    assert standard["retrieval_planner"]["max_retrieval_rounds"] == 1
    assert standard["retrieval_planner"]["verification_candidate_limit"] == 10
    assert standard["evidence_control"]["mode"] == "off"
    assert standard["answer_context"] == {
        "max_documents": 10,
        "max_chars_per_document": 2500,
        "max_total_chars": 25000,
    }

    assert thorough["search"]["rerank_candidates"] == 30
    assert thorough["search"]["final_limit"] == 30
    assert thorough["retrieval_planner"]["max_retrieval_rounds"] == 1
    assert thorough["retrieval_planner"]["verification_candidate_limit"] == 30
    assert thorough["evidence_control"]["mode"] == "off"
    assert thorough["answer_context"]["max_documents"] == 30
    assert thorough["answer_context"]["max_total_chars"] == 100000

    assert deep["search"]["final_limit"] == 50
    assert deep["retrieval_planner"]["max_retrieval_rounds"] == 1
    assert deep["retrieval_planner"]["verification_candidate_limit"] == 50
    assert deep["evidence_control"]["mode"] == "off"
    assert deep["answer_context"]["max_documents"] == 50
    assert deep["answer_context"]["max_total_chars"] == 200000

    assert standard["retrieval_planner"]["thinking"] is False
    assert thorough["retrieval_planner"]["thinking"] is False
    assert deep["retrieval_planner"]["thinking"] is False
    assert thorough["context_enrichment"]["max_chars"] > standard["context_enrichment"]["max_chars"]
    assert deep["context_enrichment"]["max_chars"] > thorough["context_enrichment"]["max_chars"]

    core_slots = {"query_rewriter", "planner", "verifier", "evidence", "answer"}
    assert core_slots.issubset(standard["prompts"])
    assert core_slots.issubset(thorough["prompts"])
    assert core_slots.issubset(deep["prompts"])
    for directory, profile in (("standard", standard), ("thorough", thorough), ("deep", deep)):
        for slot in core_slots:
            prompt_path = root / "models" / directory / "prompts" / profile["prompts"][slot]
            assert prompt_path.is_file()
            assert prompt_path.read_text(encoding="utf-8").strip()


def test_standard_prompt_pack_preserves_rc5_prompt_semantics_and_thorough_is_explicit():
    root = Path(__file__).resolve().parent.parent
    mapping = {
        "retrieval_planner.txt": "retrieval_planner.txt",
        "candidate_verifier.txt": "candidate_verifier.txt",
        "evidence_decision.txt": "evidence_decision.txt",
        "rag_answer.txt": "rag_answer.txt",
    }
    for packaged, global_name in mapping.items():
        baseline = (root / "prompts" / global_name).read_text(encoding="utf-8").strip()
        standard = (
            root / "models" / "standard" / "prompts" / packaged
        ).read_text(encoding="utf-8").strip()
        thorough = (
            root / "models" / "thorough" / "prompts" / packaged
        ).read_text(encoding="utf-8").strip()
        deep = (
            root / "models" / "deep" / "prompts" / packaged
        ).read_text(encoding="utf-8").strip()

        assert standard == baseline
        assert 'SUNAQ-PROFIL "GRÜNDLICH"' in thorough
        assert 'SUNAQ-PROFIL "TIEF"' in deep
        assert baseline in thorough
        assert baseline in deep



def test_provider_propagates_active_sunaq_model_to_auxiliary_retrieval_endpoints():
    root = Path(__file__).resolve().parent.parent
    provider = (root / "rag" / "openai_provider.py").read_text(encoding="utf-8")
    api = (root / "rag" / "api.py").read_text(encoding="utf-8")

    assert provider.count('"model": _active_model_id()') >= 5
    assert 'class ElasticSearchRequest(BaseModel):' in api
    assert 'class DocumentResolveRequest(BaseModel):' in api
    assert 'class PlanRequest(BaseModel):' in api
    assert api.count('description="SunaQ model/profile id. Omitted = configured default."') >= 5
    assert 'with use_runtime_model_config(runtime_model.config):' in api



def test_legacy_inline_settings_overlay_only_opted_in_default_model(tmp_path):
    models = tmp_path / "models"
    _write_profile(
        models,
        "standard",
        {
            "id": "sunaq-standard",
            "default": True,
            "legacy_config_overlay": True,
            "search": {"final_limit": 15, "es_limit": 50},
            "reranker": {"backend": "none"},
        },
    )
    _write_profile(
        models,
        "thorough",
        {
            "id": "sunaq-thorough",
            "search": {"final_limit": 15, "es_limit": 80},
            "reranker": {"backend": "none"},
        },
    )
    legacy = {
        "search": {"es_limit": 67, "final_limit": 9},
        "reranker": {"backend": "tei", "tei_url": "http://reranker:8080"},
        "deployment": {"profile": "standard"},
    }

    registry = load_model_registry(legacy, models_dir=models)
    standard = registry.get("sunaq-standard")
    thorough = registry.get("sunaq-thorough")

    assert standard.section("search")["es_limit"] == 67
    assert standard.section("search")["final_limit"] == 9
    assert standard.section("reranker")["backend"] == "tei"
    assert thorough.section("search")["es_limit"] == 80
    assert thorough.section("search")["final_limit"] == 15
    assert thorough.section("reranker")["backend"] == "none"



def test_profile_prompt_refactor_keeps_hybrid_web_prompt_slot_valid():
    root = Path(__file__).resolve().parent.parent
    provider = (root / "rag" / "openai_provider.py").read_text(encoding="utf-8")
    assert 'HYBRID__prompt' not in provider
    assert '_prompt("hybrid_web_answer", HYBRID_WEB_ANSWER_SYSTEM_PROMPT)' in provider



def test_provider_does_not_emit_legacy_source_id_html_comments():
    root = Path(__file__).resolve().parent.parent
    provider = (root / "rag" / "openai_provider.py").read_text(encoding="utf-8")

    # Legacy markers remain parsable for old chat history, but new normal and
    # streaming responses must not append document ids in HTML comments.
    assert 'r"<!--\\s*rag-source:' in provider
    assert ") + _source_handoff_suffix(results)" not in provider


def test_unconfigured_users_only_get_default_sunaq_model(monkeypatch):
    from types import SimpleNamespace
    import rag.openai_provider as provider

    default_model = provider.SUNAQ_MODEL_REGISTRY.default_model_id
    assert provider._model_access_for_identity(None) == ([default_model], default_model)

    class Store:
        def get_canonical_user_for_identity(self, _identity):
            return None

    monkeypatch.setattr(provider, "_provider_client_store", lambda: Store())
    assert provider._model_access_for_identity("client:user") == ([default_model], default_model)

    class KnownStore:
        def get_canonical_user_for_identity(self, _identity):
            return SimpleNamespace(canonical_user_id="user-1")

        def get_model_settings(self, _canonical_user_id):
            return None

    monkeypatch.setattr(provider, "_provider_client_store", lambda: KnownStore())
    assert provider._model_access_for_identity("client:user") == ([default_model], default_model)


def test_admin_unconfigured_user_view_matches_standard_only_entitlement():
    root = Path(__file__).resolve().parent.parent
    admin = (root / "rag" / "admin_ui.py").read_text(encoding="utf-8")
    assert "allowed = {model_registry.default_model_id}" in admin
    assert "allowed = {model.model_id for model in configured}" not in admin


def test_shipped_sunaq_models_have_stable_user_facing_order():
    root = Path(__file__).resolve().parent.parent
    registry = load_model_registry(
        yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    )
    ids = [model.model_id for model in registry.list()]
    assert ids[:3] == ["sunaq-standard", "sunaq-thorough", "sunaq-deep"]


def test_shipped_default_profile_is_named_schnell():
    root = Path(__file__).resolve().parent.parent
    standard = yaml.safe_load(
        (root / "models" / "standard" / "profile.yaml").read_text(encoding="utf-8")
    )
    assert standard["id"] == "sunaq-standard"
    assert standard["name"] == "Schnell"
    assert standard["order"] == 10


def test_nextcloud_client_renders_provider_suggestions():
    root = Path(__file__).resolve().parent.parent
    app = (root / "clients" / "nextcloud" / "sunaq" / "js" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "function runSuggestion(suggestion, messageIndex)" in app
    assert "assistant.suggestions = data.suggestions.slice(0, 4)" in app
    assert "action === 'rerun'" in app
    assert "applySuggestedModel" in app


def test_nextcloud_client_uses_compact_modern_chat_controls():
    root = Path(__file__).resolve().parent.parent
    client = root / "clients" / "nextcloud" / "sunaq"
    main = (client / "templates" / "main.php").read_text(encoding="utf-8")
    app = (client / "js" / "app.js").read_text(encoding="utf-8")
    css = (client / "css" / "style.css").read_text(encoding="utf-8")

    assert 'class="sunaq-composer-box"' in main
    assert 'class="sunaq-scope-chip"' in main
    assert 'id="sunaq-sidebar-toggle"' in main
    assert 'class="primary sunaq-send-button"' in main
    assert '>↑</button>' in main
    assert "sunaq-sidebar-collapsed" in app
    assert ".sunaq-suggestions" in css
    assert ".sunaq-composer-box:focus-within" in css


def test_nextcloud_chat_uses_one_centered_content_column():
    root = Path(__file__).resolve().parent.parent
    css = (
        root / "clients" / "nextcloud" / "sunaq" / "css" / "style.css"
    ).read_text(encoding="utf-8")
    assert "width: min(100%, 880px);" in css
    assert ".sunaq-message,\n.sunaq-message.sunaq-user" in css
    assert "width: 100%;" in css


def test_nextcloud_login_flow_presents_only_sunaq_brand():
    root = Path(__file__).resolve().parent.parent
    api = (root / "rag" / "api.py").read_text(encoding="utf-8")
    curation = (root / "rag" / "curation_ui.py").read_text(encoding="utf-8")

    assert '"User-Agent": "SunaQ"' in api
    assert '"User-Agent": "SunaQ"' in curation
    assert "Nextcloud-RAG-Middleware/" not in api
    assert "AKI-RAG/" not in curation


def test_model_id_cannot_collide_with_alias_loaded_earlier(tmp_path):
    models = tmp_path / "models"
    _write_profile(
        models,
        "a-standard",
        {
            "id": "sunaq-standard",
            "default": True,
            "aliases": ["chef"],
        },
    )
    _write_profile(
        models,
        "z-chef",
        {
            "id": "chef",
        },
    )
    with pytest.raises(RuntimeError, match="chef"):
        load_model_registry({}, models_dir=models)


def test_runtime_reranker_config_merges_profile_over_global_defaults(monkeypatch):
    from rag import search

    monkeypatch.setattr(
        search,
        "RERANKER_CONFIG",
        {"backend": "local", "batch_size": 4, "model": "base-reranker"},
    )
    with search.use_runtime_model_config({"reranker": {"batch_size": 8}}):
        effective = search._runtime_reranker_config()
    assert effective["backend"] == "local"
    assert effective["model"] == "base-reranker"
    assert effective["batch_size"] == 8


def test_identical_reranker_config_reuses_warmed_global_instance():
    from rag import reranker as reranker_module

    assert (
        reranker_module._reranker_for_config(dict(reranker_module.reranker_config))
        is reranker_module.reranker
    )


def test_only_supported_user_models_are_shipped():
    root = Path(__file__).resolve().parent.parent
    shipped = {
        item.name
        for item in (root / "models").iterdir()
        if item.is_dir() and (item / "profile.yaml").is_file()
    }
    assert shipped == {"standard", "thorough", "deep"}

    base = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    base.setdefault("architecture", {})["tier"] = "erg_full"
    base.setdefault("resolver", {})["enabled"] = True
    registry = load_model_registry(base)
    assert {model.model_id for model in registry.list()} == {
        "sunaq-standard",
        "sunaq-thorough",
        "sunaq-deep",
    }



def test_normal_models_own_query_rewriter_and_core_research_prompts():
    root = Path(__file__).resolve().parent.parent
    base = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    registry = load_model_registry(base)
    for model_id in ("sunaq-standard", "sunaq-thorough", "sunaq-deep"):
        model = registry.get(model_id)
        for prompt_name in ("query_rewriter", "planner", "verifier", "evidence", "answer"):
            assert model.prompts[prompt_name].strip()


def test_internal_document_model_semantics_are_tier_invariant():
    root = Path(__file__).resolve().parent.parent
    base = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    sections = (
        "search",
        "retrieval_planner",
        "evidence_control",
        "retrieval_signal",
        "entity_resolution",
        "graph_retrieval",
        "reranker",
        "context_enrichment",
        "answer_context",
        "resolver_research",
    )

    registries = {}
    for tier in ("erg_internal", "erg_full"):
        cfg = yaml.safe_load(yaml.safe_dump(base))
        cfg.setdefault("architecture", {})["tier"] = tier
        registries[tier] = load_model_registry(cfg)

    for model_id in ("sunaq-standard", "sunaq-thorough", "sunaq-deep"):
        internal = registries["erg_internal"].get(model_id)
        full = registries["erg_full"].get(model_id)
        assert internal.prompts == full.prompts
        for section in sections:
            assert internal.section(section) == full.section(section)


def test_provider_keeps_generic_resolver_research_path_without_register_model_package():
    root = Path(__file__).resolve().parent.parent
    provider = (root / "rag" / "openai_provider.py").read_text(encoding="utf-8")
    assert "resolver_research_enabled" in provider
    assert "resolver_direct_answer" in provider
    assert "_resolver_answer_messages" in provider
    assert "require_entity_hints=resolver_research_enabled" in provider


def test_dockerized_and_super_light_model_override_keys_are_migration_aliases(tmp_path):
    models_dir = tmp_path / "models"
    model_dir = models_dir / "standard"
    prompts = model_dir / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "answer.txt").write_text("answer")
    (model_dir / "profile.yaml").write_text(
        """
id: sunaq-standard
name: Standard
default: true
architecture_tiers: [src]
search: {}
retrieval_planner: {}
evidence_control: {}
retrieval_signal: {}
graph_retrieval:
  enabled: true
reranker:
  backend: none
context_enrichment: {}
answer_context: {}
entity_resolution: {}
roles: {}
prompts:
  answer: answer.txt
deployment_overrides:
  super-light:
    graph_retrieval:
      enabled: false
"""
    )

    cfg = {"architecture": {"tier": "src"}, "deployment": {"profile": "dockerized"}}
    model = load_model_registry(cfg, models_dir=models_dir).get()
    assert model.section("graph_retrieval")["enabled"] is False

    profile = (model_dir / "profile.yaml").read_text().replace(
        "  super-light:\n", "  dockerized:\n"
    )
    (model_dir / "profile.yaml").write_text(profile)
    cfg["deployment"]["profile"] = "super-light"
    model = load_model_registry(cfg, models_dir=models_dir).get()
    assert model.section("graph_retrieval")["enabled"] is False
