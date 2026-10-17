from erg_resolver.app import OpenRegisterResolver, OpenRegisterSettings, ReferenceInput


class _Search:
    def __init__(self):
        self.queries = []

    def find_companies_v1(self, *, query, pagination):
        self.queries.append({
            "query": dict(query),
            "pagination": dict(pagination),
        })
        results = []
        for index in range(1, 121):
            city = "Berlin" if index == 80 else "München"
            results.append({
                "company_id": f"DE-HRB-TEST-{index:03d}",
                "name": f"Kurfürstendamm Objekt {index:03d} GmbH",
                "active": True,
                "address": {
                    "formatted_value": f"Musterstraße {index}, 10000 {city}",
                    "postal_code": "10000",
                    "city": city,
                },
                "legal_form": "gmbh",
                "register_court": (
                    "Berlin (Charlottenburg)" if city == "Berlin" else "München"
                ),
                "register_number": str(index),
                "purpose": "Immobilien",
            })
        # A very strong-looking candidate beyond the configured internal
        # provider window must not be processed.
        results[109]["name"] = "Kurfürstendamm"
        return type("Response", (), {"results": results})()

    def find_person_v1(self, *, query):
        return type("Response", (), {"results": []})()


class _Client:
    def __init__(self):
        self.search = _Search()


def test_broad_company_search_ranks_local_context_before_generic_output_limit():
    client = _Client()
    resolver = OpenRegisterResolver(
        OpenRegisterSettings(candidate_fetch_limit=100),
        client=client,
    )

    rows = resolver.resolve(
        ReferenceInput(
            text="Kurfürstendamm",
            context=["Berlin"],
            hints={"kind": "organization"},
        ),
        limit=20,
    )

    assert client.search.queries == [{
        "query": {"value": "Kurfürstendamm"},
        "pagination": {"page": 1, "per_page": 100},
    }]
    assert len(rows) == 20
    assert rows[0]["candidate_id"] == "DE-HRB-TEST-080"
    assert rows[0]["attributes"]["context_hits"] == ["berlin"]
    assert rows[0]["provenance"]["context_sent_external"] is False
    assert all(row["candidate_id"] != "DE-HRB-TEST-110" for row in rows)


def test_openregister_candidate_fetch_limit_defaults_to_100():
    assert OpenRegisterSettings().candidate_fetch_limit == 100
