from pathlib import Path

from erg_resolver.app import (
    OpenPlzResolver,
    OpenPlzSettings,
    OpenRegisterResolver,
    OpenRegisterSettings,
    EnrichRequest,
    ReferenceInput,
    ResolveRequest,
    ResolverGateway,
    normalize_reference,
    read_password_file,
)


def test_normalize_reference_is_compatible_with_graph_style():
    assert normalize_reference("  Robert  Wilke, Berlin ") == "robert wilke berlin"
    assert normalize_reference("4711/25") == "4711 25"


def test_password_file_accepts_raw_and_neo4j_auth_format(tmp_path: Path):
    raw = tmp_path / "raw"
    raw.write_text("secret\n", encoding="utf-8")
    assert read_password_file(str(raw)) == "secret"

    auth = tmp_path / "auth"
    auth.write_text("neo4j/another-secret\n", encoding="utf-8")
    assert read_password_file(str(auth), "neo4j") == "another-secret"


class FakeAdapter:
    name = "fake"

    def health(self):
        return {"status": "ok"}

    def resolve(self, reference: ReferenceInput, *, limit: int):
        return [{
            "source": "fake",
            "candidate_id": "case:4711/25",
            "canonical": "Vorgang 4711/25",
            "kind": "Case",
            "aliases": ["4711/25"],
            "attributes": {"project": "Test"},
            "match": "exact",
            "confidence": 0.99,
            "provenance": {"registry": "test"},
        }][:limit]


def test_gateway_contract_with_future_adapter_shape():
    response = ResolverGateway([FakeAdapter()]).resolve(
        ResolveRequest(references=[{"text": "4711/25", "context": ["Test"]}])
    )
    assert response.results[0].status == "resolved"
    assert response.results[0].candidates[0].candidate_id == "case:4711/25"


class FakeOpenRegisterSearch:
    def __init__(self):
        self.queries = []
        self.person_queries = []

    def find_companies_v1(self, *, query, pagination):
        self.queries.append({
            "query": dict(query),
            "pagination": dict(pagination),
        })
        return type("Response", (), {
            "results": [{
                "company_id": "DE-HRB-TEST-123",
                "name": "Example GmbH",
                "active": True,
                "address": {
                    "formatted_value": "Musterstraße 1, 10117 Berlin",
                    "postal_code": "10117",
                    "city": "Berlin",
                },
                "legal_form": "gmbh",
                "register_court": "Berlin (Charlottenburg)",
                "register_number": "123",
                "purpose": "Software",
            }]
        })()

    def find_person_v1(self, *, query):
        self.person_queries.append(dict(query))
        return type("Response", (), {
            "results": [
                {
                    "id": "person-berlin",
                    "active": True,
                    "city": "Berlin",
                    "date_of_birth": "1983-04-22",
                    "name": "John Doe",
                },
                {
                    "id": "person-hamburg",
                    "active": True,
                    "city": "Hamburg",
                    "date_of_birth": "1989-05-11",
                    "name": "John Doe",
                },
            ]
        })()


class FakeOpenRegisterCompany:
    def get_details_v1(self, *, company_id: str):
        return {
            "id": company_id,
            "status": "active",
            "name": {
                "name": "Example GmbH",
                "legal_form": "gmbh",
                "start_date": "2020-01-01",
            },
            "address": {"formatted_value": "Musterstraße 1, 10117 Berlin"},
            "legal_form": "gmbh",
            "company_register": {
                "register_court": "Berlin (Charlottenburg)",
                "register_number": "123",
                "register_type": "HRB",
                "company_id": company_id,
            },
            "purpose": {"purpose": "Software"},
            "names": [
                {"name": "Example Alt GmbH", "start_date": "2010-01-01", "end_date": "2020-01-01"}
            ],
            "representation": [],
            "financials": [{
                "year": 2024,
                "revenue": 1234567,
                "currency": "EUR",
            }],
            "sources": [{"document_url": "https://signed.example.invalid/secret"}],
        }


class FakeOpenRegisterPerson:
    def get_details_v1(self, person_id: str):
        return {
            "id": person_id,
            "first_name": "Robert",
            "last_name": "Wilke",
            "city": "Berlin",
            "date_of_birth": "1983-04-22",
            "age": 43,
            "management_positions": [{
                "company_name": "R.A.W. Capital GmbH",
                "register_id": "DE-HRB-F1103-222912",
                "role": "DIRECTOR",
                "start_date": "2020-11-10",
                "end_date": None,
            }],
        }

    def get_holdings_v1(self, person_id: str):
        return {"person_id": person_id, "holdings": []}


class FakeOpenRegisterClient:
    def __init__(self):
        self.search = FakeOpenRegisterSearch()
        self.company = FakeOpenRegisterCompany()
        self.person = FakeOpenRegisterPerson()


def test_openregister_resolver_is_hint_or_company_cue_gated_and_keeps_context_local():
    client = FakeOpenRegisterClient()
    resolver = OpenRegisterResolver(OpenRegisterSettings(), client=client)

    assert resolver.resolve(ReferenceInput(text="John Doe"), limit=5) == []
    assert client.search.queries == []

    rows = resolver.resolve(
        ReferenceInput(text="Example GmbH", context=["Berlin", "secret internal context"]),
        limit=5,
    )
    assert client.search.queries == [{
        "query": {"value": "Example GmbH"},
        "pagination": {"page": 1, "per_page": 100},
    }]
    assert rows[0]["canonical"] == "Example GmbH"
    assert rows[0]["kind"] == "Organization"
    assert rows[0]["match"] == "exact_name"
    assert rows[0]["attributes"]["context_hits"] == ["berlin"]
    assert rows[0]["provenance"]["outbound_query"] == "Example GmbH"
    assert rows[0]["provenance"]["context_sent_external"] is False


def test_openregister_strips_trailing_local_context_before_external_search():
    client = FakeOpenRegisterClient()
    resolver = OpenRegisterResolver(OpenRegisterSettings(), client=client)

    rows = resolver.resolve(
        ReferenceInput(
            text="Example GmbH in Berlin",
            context=["Berlin"],
            hints={"kind": "organization"},
        ),
        limit=5,
    )

    assert client.search.queries == [{
        "query": {"value": "Example GmbH"},
        "pagination": {"page": 1, "per_page": 100},
    }]
    assert rows[0]["canonical"] == "Example GmbH"
    assert rows[0]["provenance"]["outbound_query"] == "Example GmbH"
    assert rows[0]["attributes"]["context_hits"] == ["berlin"]


def test_openregister_resolver_supports_exact_register_id():
    resolver = OpenRegisterResolver(OpenRegisterSettings(), client=FakeOpenRegisterClient())
    rows = resolver.resolve(
        ReferenceInput(text="DE-HRB-TEST-123", hints={"kind": "organization"}),
        limit=2,
    )
    assert rows[0]["candidate_id"] == "DE-HRB-TEST-123"
    assert rows[0]["match"] == "exact_register_id"
    assert rows[0]["confidence"] == 0.99


class FakeHttpResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeOpenPlzClient:
    def __init__(self):
        self.calls = []

    def close(self):
        pass

    def get(self, path, *, params):
        self.calls.append((path, dict(params)))
        if path == "/de/Localities":
            return FakeHttpResponse([{
                "name": "Bonn",
                "postalCode": "53123",
                "municipality": {"key": "05314000", "name": "Bonn, Stadt", "type": "Stadt"},
                "federalState": {"key": "05", "name": "Nordrhein-Westfalen"},
            }])
        return FakeHttpResponse([{
            "name": "Pariser Platz",
            "postalCode": "10117",
            "locality": "Berlin",
            "borough": "Mitte",
            "suburb": None,
            "municipality": {"key": "11000000", "name": "Berlin, Stadt", "type": "Stadt"},
            "federalState": {"key": "11", "name": "Berlin"},
        }])


def test_openplz_resolver_uses_locality_lookup_for_postcode_and_city():
    client = FakeOpenPlzClient()
    resolver = OpenPlzResolver(OpenPlzSettings(), client=client)
    rows = resolver.resolve(ReferenceInput(text="53123 Bonn"), limit=5)

    assert client.calls[0][0] == "/de/Localities"
    assert client.calls[0][1]["postalCode"] == "53123"
    assert rows[0]["canonical"] == "53123 Bonn"
    assert rows[0]["kind"] == "Locality"
    assert rows[0]["match"] == "exact_postal_locality"


def test_openplz_resolver_uses_fulltext_for_street_and_does_not_claim_house_number():
    client = FakeOpenPlzClient()
    resolver = OpenPlzResolver(OpenPlzSettings(), client=client)
    rows = resolver.resolve(
        ReferenceInput(text="10117 Berlin Pariser Platz", hints={"kind": "address"}),
        limit=5,
    )

    assert client.calls[0][0] == "/de/FullTextSearch"
    assert client.calls[0][1]["searchTerm"] == "10117 Berlin Pariser Platz"
    assert rows[0]["canonical"] == "Pariser Platz, 10117 Berlin"
    assert rows[0]["kind"] == "Street"
    assert rows[0]["attributes"]["house_number_verified"] is False


def test_gateway_health_reports_startup_error_without_losing_other_adapters():
    gateway = ResolverGateway([FakeAdapter()], startup_errors={"openregister": "PermissionError"})
    health = gateway.health()

    assert health["status"] == "degraded"
    assert health["adapters"]["fake"]["status"] == "ok"
    assert health["adapters"]["openregister"] == {
        "status": "error",
        "error": "PermissionError",
    }
    assert gateway.capabilities()["unavailable_adapters"] == ["openregister"]

    response = gateway.resolve(
        ResolveRequest(references=[{"text": "4711/25", "context": ["Test"]}])
    )
    assert response.results[0].status == "resolved"


def test_openregister_person_search_keeps_context_local_and_gateway_can_disambiguate():
    client = FakeOpenRegisterClient()
    resolver = OpenRegisterResolver(OpenRegisterSettings(), client=client)
    gateway = ResolverGateway([resolver])

    response = gateway.resolve(ResolveRequest(
        references=[{
            "text": "John Doe",
            "context": ["Berlin"],
            "hints": {"kind": "person"},
        }],
        sources=["openregister"],
    ))

    assert client.search.person_queries == [{"value": "John Doe"}]
    assert response.results[0].status == "resolved"
    assert response.results[0].candidates[0].candidate_id == "person-berlin"
    assert response.results[0].candidates[0].attributes["context_hits"] == ["berlin"]
    assert response.results[0].candidates[0].provenance["context_sent_external"] is False


def test_openregister_company_detail_mapping_handles_v1_nested_shape():
    resolver = OpenRegisterResolver(OpenRegisterSettings(), client=FakeOpenRegisterClient())
    rows = resolver.resolve(
        ReferenceInput(text="DE-HRB-TEST-123", hints={"kind": "organization"}),
        limit=2,
    )
    assert rows[0]["canonical"] == "Example GmbH"
    assert rows[0]["attributes"]["register_court"] == "Berlin (Charlottenburg)"
    assert rows[0]["attributes"]["register_number"] == "123"
    assert rows[0]["attributes"]["active"] is True


def test_openregister_enrichment_returns_person_positions_and_strips_signed_document_url():
    gateway = ResolverGateway([
        OpenRegisterResolver(OpenRegisterSettings(), client=FakeOpenRegisterClient())
    ])

    person = gateway.enrich(EnrichRequest(
        source="openregister",
        candidate_id="person-berlin",
        kind="Person",
    ))
    assert person.details["management_positions"][0]["company_name"] == "R.A.W. Capital GmbH"

    company = gateway.enrich(EnrichRequest(
        source="openregister",
        candidate_id="DE-HRB-TEST-123",
        kind="Organization",
    ))
    assert company.details["name"] == "Example GmbH"
    assert company.details["register_documents_available"] == 1
    assert company.details["names"][0]["name"] == "Example Alt GmbH"
    assert company.details["name_record"]["name"] == "Example GmbH"
    assert company.details["purpose_record"]["purpose"] == "Software"
    assert "source_documents" not in company.details
    assert "document_url" not in str(company.details)

    artifacts = {item.file_name: item for item in company.artifacts}
    assert set(artifacts) == {
        "details.json",
        "facts.csv",
        "openregister-source-01.pdf",
    }
    assert artifacts["details.json"].mode == "inline_text"
    assert '"financials"' in artifacts["details.json"].content
    assert artifacts["facts.csv"].mode == "inline_text"
    assert "financials[1].revenue" in artifacts["facts.csv"].content
    assert "1234567" in artifacts["facts.csv"].content
    assert artifacts["facts.csv"].include_in_summary is True
    assert "Strukturierte Registerdaten" in artifacts["facts.csv"].summary_markdown
    assert artifacts["openregister-source-01.pdf"].mode == "remote"
    assert artifacts["openregister-source-01.pdf"].url == "https://signed.example.invalid/secret"


def test_gateway_source_filter_prevents_unrequested_external_adapter_calls():
    client = FakeOpenRegisterClient()
    openregister = OpenRegisterResolver(OpenRegisterSettings(), client=client)
    response = ResolverGateway([FakeAdapter(), openregister]).resolve(
        ResolveRequest(
            references=[{"text": "Example GmbH"}],
            sources=["fake"],
        )
    )
    assert response.results[0].candidates[0].source == "fake"
    assert client.search.queries == []


def test_gateway_single_low_confidence_candidate_stays_ambiguous():
    class WeakAdapter:
        name = "weak"

        def health(self):
            return {"status": "ok"}

        def resolve(self, reference: ReferenceInput, *, limit: int):
            return [{
                "source": "weak",
                "candidate_id": "weak-1",
                "canonical": "Weak Match GmbH",
                "kind": "Organization",
                "aliases": [],
                "attributes": {},
                "match": "autocomplete",
                "confidence": 0.76,
                "provenance": {},
            }]

    response = ResolverGateway([WeakAdapter()]).resolve(
        ResolveRequest(references=[{"text": "Weak Match"}])
    )

    assert response.results[0].status == "candidates"
    assert response.results[0].candidates[0].candidate_id == "weak-1"
