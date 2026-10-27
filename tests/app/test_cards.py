"""Card validation, provenance and persistence without any paid model requests."""

import copy
import sqlite3

import pytest

from studynavigator.cards import (
    Card, CardError, analyze_assignment, confirm_card, manual_card,
    parse_card, validate_card_sources,
)
from studynavigator.catalog import CatalogError, Store, seed_database
from studynavigator.ingestion import ingest_documents
from studynavigator.mcp_gateway import MCPGateway
from test_ingestion import text_pdf


@pytest.fixture
def uploaded(tmp_path):
    path = tmp_path / "cards.sqlite"
    seed_database(path)
    assignment = ingest_documents(path, tmp_path / "uploads", "owner", [
        ("assignment.pdf", text_pdf("Build an ORION orbit simulation. Submit a notebook.",
                                     "Assess accuracy. Preparation takes 90 minutes.")),
    ])["assignment"]
    return path, Store(path, "owner"), assignment


def grounded_card(assignment):
    first, second = [source["source_id"] for source in assignment["sources"]]
    return Card(
        title="Orbit simulation",
        goal={"text": "Build an ORION orbit simulation", "origin": "document", "source_ids": [first]},
        deliverable={"text": "Submit a notebook", "origin": "document", "source_ids": [first]},
        requirements=[{"text": "Build a simulation", "origin": "document", "source_ids": [first]}],
        criteria=[{"text": "Simulation accuracy", "origin": "document", "source_ids": [second]}],
        tasks=[{"id": "prepare", "title": "Prepare the simulation", "minutes": 90,
                "estimate_source": "document", "source_ids": [second]}],
    ).model_dump()


def test_manual_draft_is_empty_and_does_not_claim_to_extract_facts():
    card = manual_card("My assignment")
    assert card["title"] == "My assignment"
    assert card["goal"] == {"text": "", "origin": "user", "source_ids": []}
    assert card["requirements"] == card["criteria"] == card["tasks"] == []
    assert card["deadline"] == {"date": None, "origin": "unknown", "source_ids": []}
    assert card["questions"]


@pytest.mark.asyncio
async def test_offline_analysis_reads_real_mcp_but_never_calls_model(uploaded):
    path, _, assignment = uploaded

    class Offline:
        mode = "offline"

        async def analyze(self, assignment):
            raise AssertionError("Offline must not claim to analyze a PDF")

    async with MCPGateway(path, workspace_id="owner") as gateway:
        result = await analyze_assignment(gateway, Offline(), assignment["assignment_id"])
    assert result["mode"] == "offline" and result["analysis_kind"] == "manual"
    assert result["card"]["requirements"] == []
    assert result["usage"] is None
    assert result["trace"][0]["name"] == "get_assignment"
    assert result["trace"][0]["status"] == "ok"


@pytest.mark.asyncio
async def test_analysis_validates_fixture_through_real_scoped_mcp(uploaded):
    path, _, assignment = uploaded

    class FixtureModel:
        mode = "live"  # Injected protocol fixture; no network or real model.

        async def analyze(self, context):
            assert set(context) == {"assignment_id", "title", "sources"}
            assert all(source["source_id"] in assignment["source_ids"] for source in context["sources"])
            return {"card": grounded_card(assignment), "usage": {"input_tokens": 100, "output_tokens": 50}}

    async with MCPGateway(path, workspace_id="owner") as gateway:
        result = await analyze_assignment(gateway, FixtureModel(), assignment["assignment_id"])
    assert result["analysis_kind"] == "model"
    assert result["card"]["deadline"]["date"] is None
    assert result["usage"] == {"input_tokens": 100, "output_tokens": 50}
    assert result["trace"][0]["name"] == "get_assignment"


@pytest.mark.asyncio
@pytest.mark.parametrize("invented", ["deadline", "duration"])
async def test_model_cannot_invent_user_constraints(uploaded, invented):
    path, _, assignment = uploaded

    class FixtureModel:
        mode = "live"

        async def analyze(self, context):
            card = grounded_card(assignment)
            if invented == "deadline":
                card["deadline"] = {"date": "2027-01-20", "origin": "user", "source_ids": []}
            else:
                card["tasks"][0].update(estimate_source="user", source_ids=[])
            return {"card": card}

    async with MCPGateway(path, workspace_id="owner") as gateway:
        with pytest.raises(CardError) as error:
            await analyze_assignment(gateway, FixtureModel(), assignment["assignment_id"])
    assert error.value.code == "invalid_provenance"


@pytest.mark.parametrize("mutation,code", [
    (lambda c: c["goal"].update(source_ids=["unknown-source"]), "unsupported_source"),
    (lambda c: c["goal"].update(source_ids=[]), "missing_sources"),
    (lambda c: c["goal"].update(origin="user"), "invalid_provenance"),
    (lambda c: c["deadline"].update(date="2027-02-30", origin="user"), "invalid_card"),
    (lambda c: c["deadline"].update(date="2027-02-20"), "invalid_card"),
    (lambda c: c["tasks"][0].update(minutes=True), "invalid_card"),
    (lambda c: c["tasks"].append(copy.deepcopy(c["tasks"][0])), "invalid_card"),
    (lambda c: c.update(unexpected="not allowed"), "invalid_card"),
])
def test_bad_cards_are_rejected_before_persistence(uploaded, mutation, code):
    _, _, assignment = uploaded
    card = grounded_card(assignment)
    mutation(card)
    with pytest.raises(CardError) as error:
        validate_card_sources(card, assignment["sources"])
    assert error.value.code == code


def test_confirmation_preserves_only_unchanged_document_facts(uploaded):
    _, _, assignment = uploaded
    draft = grounded_card(assignment)
    draft["deadline"] = {"date": "2027-01-20", "origin": "document",
                          "source_ids": [assignment["source_ids"][0]]}
    proposed = copy.deepcopy(draft)
    proposed["goal"]["text"] = "My revised goal"
    proposed["deadline"]["date"] = "2027-01-25"
    confirmed = confirm_card(proposed, draft, assignment["sources"])
    assert confirmed["goal"] == {"text": "My revised goal", "origin": "user", "source_ids": []}
    assert confirmed["deliverable"] == draft["deliverable"]
    assert confirmed["requirements"] == draft["requirements"]
    assert confirmed["deadline"] == {"date": "2027-01-25", "origin": "user", "source_ids": []}
    assert confirmed["tasks"] == []  # The old task proposal no longer matches the goal.


def test_confirmation_does_not_promote_new_document_claims_or_time_estimates(uploaded):
    _, _, assignment = uploaded
    draft = grounded_card(assignment)
    proposed = copy.deepcopy(draft)
    proposed["criteria"].append({"text": "User-added criterion", "origin": "document",
                                 "source_ids": [assignment["source_ids"][0]]})
    proposed["tasks"][0]["minutes"] = 120
    confirmed = confirm_card(proposed, draft, assignment["sources"])
    assert confirmed["criteria"][-1] == {"text": "User-added criterion", "origin": "user", "source_ids": []}
    assert confirmed["tasks"][0]["estimate_source"] == "user"
    assert confirmed["tasks"][0]["source_ids"] == []
    assert confirmed["tasks"][0]["minutes"] == 120


def test_confirmation_rejects_foreign_sources_even_on_a_changed_user_field(uploaded):
    _, _, assignment = uploaded
    draft = grounded_card(assignment)
    proposed = copy.deepcopy(draft)
    proposed["goal"] = {"text": "Modified", "origin": "document", "source_ids": ["foreign-source"]}
    with pytest.raises(CardError) as error:
        confirm_card(proposed, draft, assignment["sources"])
    assert error.value.code == "unsupported_source"


def test_confirmation_requires_goal_and_deliverable_but_unknown_deadline_is_valid(uploaded):
    _, _, assignment = uploaded
    card = manual_card("My assignment")
    with pytest.raises(CardError) as error:
        confirm_card(card, card, assignment["sources"])
    assert error.value.code == "incomplete_card"
    card["goal"]["text"], card["deliverable"]["text"] = "Understand orbits", "A notebook"
    confirmed = confirm_card(card, None, assignment["sources"])
    assert confirmed["deadline"]["date"] is None


def test_draft_confirmation_and_restart_keep_generic_provenance(uploaded):
    path, store, assignment = uploaded
    original = Store(path).get_assignment("personal-project")
    card = grounded_card(assignment)
    selected = assignment["assignment_id"]
    draft = store.save_card(selected, card, expected_revision=0)["assignment"]
    assert draft["status"] == "needs_confirmation" and draft["card_revision"] == 1
    assert draft["requirements"] == []
    normalized = confirm_card(card, draft["card"], draft["sources"])
    ready = store.save_card(selected, normalized, confirmed=True, expected_revision=1)["assignment"]
    assert ready["status"] == "ready" and ready["card_revision"] == 2
    assert ready["title"] == card["title"]
    assert ready["requirements"][0]["origin"] == "document"
    assert ready["criteria"][0]["title"] == "Simulation accuracy"
    assert ready["facts"] == [] and ready["readme_sections"] == []
    assert ready["due_date"] is None and ready["due_at"] is None
    seed_database(path)
    assert store.get_assignment(selected)["assignment"] == ready
    assert Store(path).get_assignment("personal-project") == original
    assert all("card" not in item for item in store.list_assignments()["assignments"])


def test_stale_revision_and_foreign_workspace_cannot_overwrite_card(uploaded):
    path, store, assignment = uploaded
    selected, card = assignment["assignment_id"], grounded_card(assignment)
    store.save_card(selected, card, expected_revision=0)
    with pytest.raises(CardError) as error:
        store.save_card(selected, card, confirmed=True, expected_revision=0)
    assert error.value.code == "card_conflict" and error.value.status_code == 409
    with pytest.raises(CatalogError) as error:
        Store(path, "stranger").save_card(selected, card, expected_revision=1)
    assert error.value.code == "unknown_assignment"
    assert store.get_assignment(selected)["assignment"]["card_revision"] == 1


def test_seed_card_is_read_only(uploaded):
    _, store, _ = uploaded
    with pytest.raises(CardError) as error:
        store.save_card("personal-project", manual_card("Example"))
    assert error.value.status_code == 403


def test_failed_confirmation_rolls_back_metadata_card_and_requirements(uploaded):
    path, store, assignment = uploaded
    selected, card = assignment["assignment_id"], grounded_card(assignment)
    store.save_card(selected, card, confirmed=True, expected_revision=0)
    before = store.get_assignment(selected)
    edited = copy.deepcopy(card)
    edited["title"] = "Changed title"
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TRIGGER fail_card BEFORE INSERT ON requirements
            BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        store.save_card(selected, edited, confirmed=True, expected_revision=1)
    assert store.get_assignment(selected) == before
