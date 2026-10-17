from rag.openregister_graph import OpenRegisterGraphStore, _candidate_state


class _Record:
    def __init__(self, value):
        self.value = value

    def data(self):
        return self.value


class _Session:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def run(self, query, **params):
        self.calls.append((query, params))
        return []


class _Driver:
    def __init__(self):
        self.calls = []

    def session(self, **kwargs):
        return _Session(self.calls)

    def close(self):
        return None


def test_candidate_state_distinguishes_selected_candidate_and_bycatch():
    assert _candidate_state("resolved", 1) == "selected"
    assert _candidate_state("candidates", 1) == "candidate"
    assert _candidate_state("candidates", 2) == "bycatch"
    assert _candidate_state("resolved", 2) == "bycatch"


def test_openregister_graph_persists_enrichment_roles_and_bycatch():
    driver = _Driver()
    store = OpenRegisterGraphStore(
        {"neo4j": {"enabled": True, "database": "neo4j"}},
        driver=driver,
    )
    result = store.persist(
        run_id="run-1",
        resolver_response={
            "results": [{
                "reference": "Example",
                "status": "candidates",
                "candidates": [
                    {
                        "source": "openregister",
                        "candidate_id": "DE-HRB-1",
                        "canonical": "Example AG",
                        "kind": "Organization",
                        "attributes": {},
                        "match": "prefix_name",
                        "confidence": 0.86,
                    },
                    {
                        "source": "openregister",
                        "candidate_id": "DE-HRB-2",
                        "canonical": "Example Holding GmbH",
                        "kind": "Organization",
                        "attributes": {},
                        "match": "autocomplete",
                        "confidence": 0.72,
                    },
                ],
            }]
        },
        enrichments=[{
            "candidate": {
                "source": "openregister",
                "candidate_id": "DE-HRB-1",
                "canonical": "Example AG",
                "kind": "Organization",
            },
            "response": {
                "source": "openregister",
                "candidate_id": "DE-HRB-1",
                "kind": "Organization",
                "details": {
                    "id": "DE-HRB-1",
                    "name": "Example AG",
                    "status": "active",
                    "names": [
                        {"name": "Example alt AG", "start_date": "2000-01-01", "end_date": "2010-01-01"}
                    ],
                    "representation": [{
                        "id": "person-1",
                        "name": "Max Beispiel",
                        "type": "natural_person",
                        "role": "DIRECTOR",
                        "start_date": "2010-01-01",
                        "end_date": None,
                    }],
                },
            },
            "error": "",
        }],
        documents=[{
            "entity": "Example AG",
            "candidate_id": "DE-HRB-1",
            "category": "provider_source",
            "status": "archived",
            "response": {
                "file_name": "openregister-source-01.pdf",
                "path": "SunaQ-Chats/x/runs/y/sources/openregister/DE-HRB-1/openregister-source-01.pdf",
                "file_id": "123",
                "source_url": "https://nextcloud.example/apps/files/files/123",
                "sha256": "a" * 64,
            },
        }],
    )

    assert result["findings"] == 2
    assert result["documents"] == 1
    assert result["facts"] >= 2

    finding_states = [
        params.get("selection_status")
        for _query, params in driver.calls
        if "selection_status" in params
    ]
    assert "candidate" in finding_states
    assert "bycatch" in finding_states

    role_calls = [
        params
        for query, params in driver.calls
        if "OPENREGISTER_ROLE" in query
    ]
    assert role_calls
    assert role_calls[0]["role"] == "DIRECTOR"
    assert role_calls[0]["current"] is True
