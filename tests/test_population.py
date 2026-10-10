"""Tests for population conditioning (althing.population) and the global-respondents pack."""

from __future__ import annotations

from collections import Counter

import pytest

from althing.population import (
    PopulationError,
    allocate_population,
    filter_personas,
    load_pack_personas,
    parse_population,
)
from althing.prompts import persona_system_prompt

PERSONAS = [
    {"name": "Respondent from Brazil", "country": "Brazil"},
    {"name": "Respondent from France", "country": "France", "gender": "female"},
    {"name": "Respondent from Japan", "country": "Japan"},
    {"name": "No country"},
]


def test_parse_population():
    assert parse_population("country=France, Japan;gender=female") == {
        "country": ["France", "Japan"],
        "gender": ["female"],
    }


@pytest.mark.parametrize("spec", ["", "country", "country=", "=France"])
def test_parse_population_rejects_bad_specs(spec):
    with pytest.raises(PopulationError):
        parse_population(spec)


def test_filter_personas_matches_all_keys_case_insensitively():
    kept = filter_personas(PERSONAS, {"country": ["france", "JAPAN"]})
    assert [p["country"] for p in kept] == ["France", "Japan"]
    kept = filter_personas(PERSONAS, {"country": ["France", "Japan"], "gender": ["female"]})
    assert [p["country"] for p in kept] == ["France"]


def test_filter_personas_names_missing_values():
    with pytest.raises(PopulationError, match="Atlantis"):
        filter_personas(PERSONAS, {"country": ["Atlantis"]})


def test_allocate_population_even_split():
    alloc = allocate_population(PERSONAS, key="country", members=["Brazil", "France", "Japan"], n_samples=30, seed="q1")
    assert sum(n for _, n in alloc) == 30
    assert {p["country"]: n for p, n in alloc} == {"Brazil": 10, "France": 10, "Japan": 10}


def test_allocate_population_weights_repeated_members():
    alloc = allocate_population(
        PERSONAS, key="country", members=["Brazil", "Brazil", "France", "Japan"], n_samples=40, seed="q1"
    )
    assert {p["country"]: n for p, n in alloc} == {"Brazil": 20, "France": 10, "Japan": 10}


def test_allocate_population_more_members_than_samples():
    personas = [{"name": f"P{i}", "country": f"C{i:02d}"} for i in range(50)]
    alloc = allocate_population(personas, key="country", members=[f"C{i:02d}" for i in range(50)], n_samples=30, seed=7)
    assert len(alloc) == 30
    assert all(n == 1 for _, n in alloc)


def test_allocate_population_is_deterministic_per_seed():
    personas = [{"name": f"P{i}", "country": f"C{i:02d}"} for i in range(50)]
    members = [p["country"] for p in personas]

    def draw(seed):
        alloc = allocate_population(personas, key="country", members=members, n_samples=10, seed=seed)
        return [p["country"] for p, _ in alloc]

    assert draw("a") == draw("a")
    assert draw("a") != draw("b")


def test_allocate_population_rejects_unknown_members():
    with pytest.raises(PopulationError, match="Atlantis"):
        allocate_population(PERSONAS, key="country", members=["Brazil", "Atlantis"], n_samples=4)


def test_persona_prompt_states_demographic_attributes():
    prompt = persona_system_prompt({"name": "Respondent from France", "country": "France", "gender": "female"})
    assert "Country: France." in prompt
    assert "Gender: female." in prompt


def test_persona_prompt_unchanged_without_attributes():
    persona = {"name": "Sarah", "age": 34, "occupation": "PM", "background": "Works at a SaaS company"}
    assert persona_system_prompt(persona) == (
        "You are role-playing as Sarah. Age: 34. Occupation: PM. Background: Works at a SaaS company. "
        "Answer questions in character. Be authentic to this persona's perspective, experiences, "
        "and communication style. Give concise, direct answers."
    )


def test_global_respondents_pack():
    personas = load_pack_personas("global-respondents")
    countries = [p["country"] for p in personas]
    assert len(countries) == len(set(countries)) >= 100
    assert {"United States", "Great Britain", "South Korea", "Brazil", "Japan"} <= set(countries)
    assert all(set(p) == {"name", "country", "background"} for p in personas)
    assert Counter(p["name"] for p in personas).most_common(1)[0][1] == 1
