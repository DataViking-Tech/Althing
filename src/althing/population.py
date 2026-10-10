"""Population conditioning: pick and weight personas to match a target population.

A *population pack* is an ordinary persona pack whose personas carry
structured attributes (``country: France``). A *population* is a
specification of who should answer, in one of two shapes:

* a filter, ``{"country": ["France", "Japan"]}`` — keep the personas whose
  attribute is one of the listed values (:func:`filter_personas`); used by
  ``althing panel run --population``;
* a member list, ``["Brazil", "Brazil", "Japan"]`` on one attribute — each
  entry is one unit of the target population, repeats included, and
  :func:`allocate_population` splits a sample budget across them. This is
  how a benchmark reproduces a ground truth that averages over groups:
  each group gets an equal share of samples, and a group listed twice gets
  twice the share.

Allocation is deterministic for a given seed, so a benchmark question
always conditions on the same personas.
"""

from __future__ import annotations

import random
from collections import Counter
from typing import Any


class PopulationError(ValueError):
    """Raised when a population cannot be matched to a pack's personas."""


def parse_population(spec: str) -> dict[str, list[str]]:
    """Parse ``"country=France,Japan;gender=female"`` into a filter dict."""
    population: dict[str, list[str]] = {}
    for clause in spec.split(";"):
        clause = clause.strip()
        if not clause:
            continue
        key, sep, values = clause.partition("=")
        key = key.strip()
        vals = [v.strip() for v in values.split(",") if v.strip()]
        if not sep or not key or not vals:
            raise PopulationError(f"Invalid population clause {clause!r}; expected KEY=VALUE[,VALUE...]")
        population.setdefault(key, []).extend(vals)
    if not population:
        raise PopulationError("Population spec is empty")
    return population


def _attr(persona: dict[str, Any], key: str) -> str | None:
    value = persona.get(key)
    return None if value is None else str(value)


def filter_personas(personas: list[dict[str, Any]], population: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Keep personas whose attributes match every key in *population*.

    Matching is case-insensitive on the attribute value. A persona without
    the attribute never matches. Raises :class:`PopulationError` when
    nothing matches, naming the values the pack does not cover.
    """
    wanted = {k: {v.casefold() for v in vals} for k, vals in population.items()}
    kept = [p for p in personas if all((_attr(p, k) or "").casefold() in vals for k, vals in wanted.items())]
    if not kept:
        missing = {
            k: sorted(v for v in vals if all((_attr(p, k) or "").casefold() != v.casefold() for p in personas))
            for k, vals in population.items()
        }
        raise PopulationError(f"No personas match population {population}; values not in pack: {missing}")
    return kept


def allocate_population(
    personas: list[dict[str, Any]],
    *,
    key: str,
    members: list[str],
    n_samples: int,
    seed: str | int | None = None,
) -> list[tuple[dict[str, Any], int]]:
    """Split *n_samples* across population *members* and resolve them to personas.

    Each member (repeats included) is resolved to the persona whose *key*
    attribute equals it (case-insensitive). Members are shuffled with
    *seed* and dealt samples round-robin, so every member gets
    ``n // m`` or ``n // m + 1`` samples; when there are more members than
    samples, the shuffle decides which are drawn. Samples landing on the
    same persona are summed.

    Returns ``(persona, n)`` pairs, persona order following first draw.
    Raises :class:`PopulationError` if any member has no matching persona.
    """
    if not members:
        raise PopulationError("Population has no members")
    by_value: dict[str, dict[str, Any]] = {}
    for p in personas:
        value = _attr(p, key)
        if value is not None:
            by_value.setdefault(value.casefold(), p)
    unknown = sorted({m for m in members if m.casefold() not in by_value})
    if unknown:
        raise PopulationError(f"No persona with {key} matching: {unknown}")

    order = sorted(members)
    random.Random(seed).shuffle(order)
    totals: Counter[int] = Counter()
    first_seen: list[dict[str, Any]] = []
    for i in range(n_samples):
        persona = by_value[order[i % len(order)].casefold()]
        if id(persona) not in totals:
            first_seen.append(persona)
        totals[id(persona)] += 1
    return [(p, totals[id(p)]) for p in first_seen]


def load_pack_personas(pack_id: str) -> list[dict[str, Any]]:
    """Load and validate the personas of a bundled or installed pack."""
    from althing.mcp.data import get_persona_pack, validate_persona_pack

    return validate_persona_pack(get_persona_pack(pack_id).get("personas", []))
