"""Weighted per-persona model split.

Shared by the CLI (``panel run --models a:0.5,b:0.5`` and ``--model auto``),
the MCP server and the SDK, so a blend chosen by auto model routing splits
personas the same way on every surface.
"""

from __future__ import annotations

from typing import Any


def assign_models_to_personas(
    personas: list[dict[str, Any]],
    model_spec: list[tuple[str, float]],
    default_model: str,
) -> tuple[dict[str, str], list[str]]:
    """Assign models to personas using Hamilton's largest-remainder method.

    Resolution order: persona YAML 'model' field > weighted assignment > default.

    Algorithm (fully deterministic — same inputs produce the same output):

    1. **YAML override pass.** Walk ``personas`` in list order. Any persona
       with a ``model`` field keeps that model and is removed from the
       assignment pool. This override takes precedence over the ``--models``
       spec unconditionally.
    2. **Normalize weights.** Compute ``total = sum(weights)`` and divide each
       weight by it. Absolute weights don't matter, only ratios — so
       ``"a:0.5,b:0.5"``, ``"a:1,b:1"`` and ``"a:50,b:50"`` all behave the
       same.
    3. **Largest-remainder allocation.** Floor each ``weight * N`` quota,
       then hand out leftover seats in descending order of fractional
       remainder. Ties are broken deterministically by position in the spec.
    4. **Min-1 guarantee** (sp-27rz). When ``N >= len(model_spec)``, every
       model in ``model_spec`` is guaranteed at least one persona: any
       zero-count model pulls a seat from the currently largest bucket.
       The prior round-based allocation could silently hand the last model
       0 personas, dropping it from per_model_results without any warning.
       When there are fewer personas than models, affected models still
       end up at zero and a warning is appended to the returned list so
       callers can surface it to the user.

    Edge cases:

    * **Empty pool** (every persona has a YAML ``model`` override):
      ``model_spec`` is effectively ignored and the YAML map is returned.
    * **Weights that don't sum to 1.0** (e.g. ``"a:2,b:3"``): normalized
      internally, so the split still works. The CLI warns about this so
      the user knows the ratio they intended is what got applied.
    * **Zero weight**: rejected upstream by :func:`parse_models_spec`.
    * **``default_model``**: currently unused by the algorithm — YAML
      override and the ``--models`` spec between them cover every persona.
      Kept in the signature so callers can pass the CLI's fallback for
      future use.

    Returns a ``(assignments, warnings)`` tuple.
    """
    warnings: list[str] = []
    needs_assignment: list[str] = []
    result: dict[str, str] = {}
    for p in personas:
        name = p.get("name", "Anonymous")
        # sp-4loufu: accept ``llm_overrides.model`` as an equivalent to
        # the legacy top-level ``model`` field. Top-level wins so
        # existing YAML keeps its behaviour.
        explicit_model = p.get("model") or (p.get("llm_overrides") or {}).get("model")
        if explicit_model:
            result[name] = explicit_model
        else:
            needs_assignment.append(name)

    if not needs_assignment:
        return result, warnings

    n = len(needs_assignment)
    total_weight = sum(w for _, w in model_spec)
    normalized = [(m, w / total_weight) for m, w in model_spec]

    # Hamilton's method (largest-remainder): floor each quota, then hand out
    # leftover seats in descending order of fractional remainder. Deterministic
    # ties broken by position in the spec.
    quotas = [w * n for _, w in normalized]
    counts = [int(q) for q in quotas]
    remainders = [q - int(q) for q in quotas]
    seats_left = n - sum(counts)
    order = sorted(range(len(counts)), key=lambda i: (-remainders[i], i))
    for i in order[:seats_left]:
        counts[i] += 1

    # Min-1 guarantee: when there are enough personas to cover every model,
    # ensure no model is left at zero by taking one seat from the currently
    # largest bucket. Preserves proportions as closely as possible while
    # eliminating the silent-drop bug.
    if n >= len(model_spec):
        for zi, c in enumerate(counts):
            if c != 0:
                continue
            donor = max(
                range(len(counts)),
                key=lambda i: (counts[i], -i),
            )
            if counts[donor] <= 1:
                break
            counts[donor] -= 1
            counts[zi] += 1

    # Any remaining zeros mean personas < models — surface loudly.
    for (model, _), c in zip(normalized, counts):
        if c == 0:
            warnings.append(
                f"model_allocation: '{model}' received 0 of {n} personas "
                f"(fewer personas than weighted models); it will be absent "
                f"from per_model_results"
            )

    idx = 0
    for (model, _), c in zip(normalized, counts):
        for _ in range(c):
            result[needs_assignment[idx]] = model
            idx += 1

    return result, warnings
