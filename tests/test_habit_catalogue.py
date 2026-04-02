"""Tests for habit catalogue and focus plan endpoint."""

import pytest

from engine.coaching.habit_catalogue import (
    HABITS,
    get_habits_by_category,
    get_habit_by_id,
    get_all_categories,
)


class TestHabitCatalogue:
    def test_all_habits_have_required_fields(self):
        for h in HABITS:
            assert "id" in h, f"Missing id in {h.get('action', 'unknown')}"
            assert "action" in h
            assert "category" in h
            assert "purpose" in h
            assert "citations" in h
            assert len(h["citations"]) >= 1, f"Habit {h['id']} has no citations"

    def test_all_citations_have_pmid(self):
        for h in HABITS:
            for c in h["citations"]:
                assert "pmid" in c, f"Habit {h['id']} citation missing pmid"
                assert "url" in c, f"Habit {h['id']} citation missing url"
                assert "title" in c, f"Habit {h['id']} citation missing title"
                assert c["url"].startswith("https://pubmed.ncbi.nlm.nih.gov/"), \
                    f"Habit {h['id']} citation URL not PubMed"

    def test_all_habit_ids_unique(self):
        ids = [h["id"] for h in HABITS]
        assert len(ids) == len(set(ids)), "Duplicate habit IDs found"

    def test_all_pmids_are_numeric_strings(self):
        for h in HABITS:
            for c in h["citations"]:
                assert c["pmid"].isdigit(), f"PMID {c['pmid']} is not numeric for {h['id']}"

    def test_categories_cover_expected_set(self):
        cats = get_all_categories()
        expected = {"sleep", "nutrition", "movement", "stress", "social", "mental", "medical"}
        assert expected.issubset(set(cats)), f"Missing categories: {expected - set(cats)}"

    def test_get_habits_by_category(self):
        sleep = get_habits_by_category("sleep")
        assert len(sleep) >= 3
        assert all(h["category"] == "sleep" for h in sleep)

    def test_get_habit_by_id(self):
        h = get_habit_by_id("nutrition-protein-first-meal")
        assert h is not None
        assert h["action"] == "eat 30g protein at your first meal"
        assert h["citations"][0]["pmid"] == "24477298"

    def test_get_habit_by_id_nonexistent(self):
        assert get_habit_by_id("nonexistent") is None

    def test_minimum_catalogue_size(self):
        """Catalogue should have at least 15 habits to cover common recommendations."""
        assert len(HABITS) >= 15


class TestCitationValidation:
    def test_validate_citations_replaces_hallucinated(self):
        """If LLM returns a catalogueId, citations should be replaced with catalogue versions."""
        from engine.gateway.focus_plan_api import _validate_citations

        result = {
            "primaryRecommendation": {
                "catalogueId": "nutrition-protein-first-meal",
                "evidence": [{"title": "FAKE PAPER", "pmid": "0000000"}],
            },
            "alternatives": [],
        }
        _validate_citations(result)

        # Should have been replaced with the real citation
        assert result["primaryRecommendation"]["evidence"][0]["pmid"] == "24477298"
        assert "FAKE" not in result["primaryRecommendation"]["evidence"][0]["title"]

    def test_validate_citations_drops_unknown_id(self):
        """If catalogueId doesn't exist, evidence should be empty."""
        from engine.gateway.focus_plan_api import _validate_citations

        result = {
            "primaryRecommendation": {
                "catalogueId": "nonexistent-habit",
                "evidence": [{"title": "anything"}],
            },
            "alternatives": [],
        }
        _validate_citations(result)
        assert result["primaryRecommendation"]["evidence"] == []
