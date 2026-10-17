"""Backend selection belongs to model profiles, never to LLM/planner text."""

from dataclasses import dataclass, field
import pytest

from rag.research.backends import load_research_backend_registry
from rag.research.registry import DelegatedResearchSettings


@dataclass
class Model:
    model_id: str = "sunaq-test"
    values: dict = field(default_factory=dict)

    def section(self, name):
        return self.values.get(name, {})


def registry():
    return load_research_backend_registry({
        "research_backends": {
            "brave-search": {
                "kind": "web_retrieval",
                "adapter": "brave_search",
                "enabled": True,
            },
            "openai-research": {
                "kind": "delegated_research",
                "adapter": "openai",
                "enabled": True,
                "model": "gpt-test",
                "api_key_env": "LLM_API_KEY",
            },
            "future-gemini": {
                "kind": "delegated_research",
                "adapter": "gemini",
                "enabled": False,
            },
        }
    })


def legacy(enabled=True):
    return DelegatedResearchSettings(
        enabled=enabled, provider="openai" if enabled else "native",
        model="gpt-legacy", delegate_web_only=True,
    )


def resolve(m, *, native=True, enabled=True):
    return registry().resolve(m, legacy=legacy(enabled), native_enabled=native)


def test_model_binding_overrides_global_legacy_openai_without_changing_role_llm():
    model = Model(values={
        "capabilities": {"documents": True, "web": True},
        "research": {"web_backend": "brave-search"},
    })
    backend = resolve(model)
    assert backend.backend_id == "brave-search"
    assert backend.kind == "web_retrieval"
    assert backend.settings is None


def test_different_model_can_select_openai_with_backend_specific_model():
    model = Model(values={"research": {"web_backend": "openai-research"}})
    backend = resolve(model)
    assert backend.delegated is True
    assert backend.settings.model == "gpt-test"
    assert backend.settings.api_key_env == "LLM_API_KEY"


def test_old_unbound_models_keep_existing_global_delegated_behavior():
    selected = resolve(Model())
    assert selected.backend_id == "legacy-delegated"
    assert selected.settings.model == "gpt-legacy"
    assert resolve(Model(), enabled=False).backend_id == "legacy-native"


def test_explicit_internal_only_model_forbids_web_even_if_legacy_openai_active():
    model = Model(values={
        "capabilities": {"documents": True, "web": False},
        "research": {"web_backend": "openai-research"},
    })
    assert resolve(model) is None


def test_none_backend_disables_web_even_when_engine_is_available():
    model = Model(values={"research": {"web_backend": "none"}})
    assert resolve(model) is None


def test_disabled_or_missing_backend_fails_closed_with_no_fallback():
    for name in ["future-gemini", "missing-provider"]:
        with pytest.raises(ValueError):
            resolve(Model(values={"research": {"web_backend": name}}))


def test_native_adapter_disabled_in_global_web_configuration_fails_closed():
    model = Model(values={"research": {"web_backend": "brave-search"}})
    with pytest.raises(ValueError, match="native Web engine is disabled"):
        resolve(model, native=False)


def test_backend_names_and_credentials_are_strictly_validated():
    with pytest.raises(ValueError, match="invalid research backend id"):
        load_research_backend_registry({"research_backends": {"../evil": {
            "kind": "delegated_research", "adapter": "openai"
        }}})
    with pytest.raises(ValueError, match="invalid api_key_env"):
        load_research_backend_registry({"research_backends": {"x": {
            "kind": "delegated_research", "adapter": "openai", "api_key_env": "abc; export SECRET=..."
        }}})
    with pytest.raises(ValueError, match="unsupported kind/adapter"):
        load_research_backend_registry({"research_backends": {"x": {
            "kind": "delegated_research", "adapter": "gemini", "enabled": True
        }}})


def test_model_capabilities_require_strict_boolean_values():
    with pytest.raises(ValueError, match="invalid capability"):
        resolve(Model(values={"capabilities": {"web": "true"}}))


def test_profile_backend_controls_provider_selection(monkeypatch):
    import rag.openai_provider as provider
    monkeypatch.setattr(provider, "RESEARCH_BACKEND_REGISTRY", registry())
    monkeypatch.setattr(provider, "_provider_web_config", lambda: {"enabled": True})
    monkeypatch.setattr(provider, "DELEGATED_RESEARCH_SETTINGS", legacy())

    brave = Model(values={"research": {"web_backend": "brave-search"}})
    openai = Model(values={"research": {"web_backend": "openai-research"}})
    assert provider._research_backend_for_model(brave).adapter == "brave_search"
    assert provider._research_backend_for_model(openai).adapter == "openai"


def test_source_capabilities_change_with_selected_model(monkeypatch, tmp_path):
    import rag.openai_provider as provider
    from rag.credential_store import CredentialStore, scope_identity

    store = CredentialStore(tmp_path / "users.sqlite")
    identity = scope_identity("test-client", "alice-ext")
    user = store.bind_identity(identity, "https://cloud.example", "alice")
    store.set_web_settings(
        user.canonical_user_id,
        enabled=True,
        archive_enabled=False,
        target_path="Research/Web",
    )

    monkeypatch.setattr(provider, "_PROVIDER_CLIENT_STORE", store)
    monkeypatch.setattr(provider, "RESEARCH_BACKEND_REGISTRY", registry())
    monkeypatch.setattr(provider, "_provider_web_config", lambda: {"enabled": True})
    monkeypatch.setattr(provider, "DELEGATED_RESEARCH_SETTINGS", legacy())

    internal = Model(values={"capabilities": {"web": False}})
    brave = Model(values={"research": {"web_backend": "brave-search"}})
    delegated = Model(values={"research": {"web_backend": "openai-research"}})

    assert provider._source_capabilities_for_identity(identity, model=internal)["web"] is False
    assert provider._source_capabilities_for_identity(identity, model=brave)["web"] is True
    assert provider._source_capabilities_for_identity(identity, model=delegated)["web"] is True
