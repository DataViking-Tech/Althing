"""Auto model routing from SynthBench ``routing.json``.

``--model auto`` (CLI), ``model="auto"`` (MCP ``run_panel`` /
``run_quick_poll``) and ``run_panel(model="auto")`` (SDK) all call
:func:`route_models`, so the policy lives in one place (same pattern as
:mod:`althing.llm.fast_default`).

The policy, in order:

1. **Slice** (who is answering). An explicit ``use_case`` wins. Otherwise a
   non-US ``country`` in the population, or the ``global-respondents``
   pack, means ``cross-national``; US demographic attributes in the
   population, or a US subgroup pack, mean ``us-subgroups``; anything else
   is ``us-general``.
2. **Topic** (what the questions are about). An explicit ``topic`` wins.
   Otherwise each question is matched against the taxonomy keywords in
   routing.json (whole words and phrases, never substrings) and the topic
   that more than half of the questions fall under is used. Without a clear
   majority the topic is ``all``.
3. **Selection.** The route for (slice, topic) supplies ``best_althing``,
   the best Althing-conditioned config. When that route is not
   ``confident`` (or missing), the slice's ``all`` route is used; when that
   is not confident either, its top Althing-conditioned candidate is still
   used (``confident=False``, ``fallback="slice-top"``), because a default
   model SynthBench ranks lower is not a better guess. The caller's default
   model is used only when there is no routing data or nothing can run. Members
   that cannot run here (no mapping, or no credentials for their provider)
   are dropped; a blend that loses members is renormalized, and a blend
   left with one member becomes a single model.

The result is a :class:`RouteDecision` with either ``model`` (one model) or
``model_spec`` (a weighted blend, executed as a per-persona split), plus the
slice, topic, confidence, data source and a one-sentence reason.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from althing import synthbench

AUTO_MODEL = "auto"
TOPIC_ALL = "all"

SLICE_US_GENERAL = "us-general"
SLICE_US_SUBGROUPS = "us-subgroups"
SLICE_CROSS_NATIONAL = "cross-national"

# Slices and topics from routing contract v1. Used to validate explicit
# ``use_case`` / ``topic`` values when routing.json is unavailable; when it
# loads, the document's own lists are authoritative.
DEFAULT_SLICES: dict[str, str] = {
    SLICE_US_GENERAL: "US general public",
    SLICE_US_SUBGROUPS: "US demographic subgroups",
    SLICE_CROSS_NATIONAL: "Respondents outside the US",
}
DEFAULT_TOPICS: dict[str, str] = {
    "politics": "Politics and policy",
    "economy": "Economy and work",
    "technology": "Technology and media",
    "science-health": "Science and health",
    "identity": "Identity and values",
    "society": "Family, community and safety",
    "global": "Global affairs",
}

# Persona packs that signal a slice on their own.
CROSS_NATIONAL_PACKS: frozenset[str] = frozenset({"global-respondents"})
US_SUBGROUP_PACKS: frozenset[str] = frozenset({"us-subgroups", "us-demographics", "us-respondents"})

_US_COUNTRY_VALUES: frozenset[str] = frozenset(
    {
        "us",
        "u.s.",
        "usa",
        "u.s.a.",
        "united states",
        "united states of america",
        "america",
    }
)
_COUNTRY_KEYS: frozenset[str] = frozenset({"country", "nation", "country_name"})
# Population attributes that describe a US demographic subgroup.
US_DEMOGRAPHIC_KEYS: frozenset[str] = frozenset(
    {
        "age",
        "age_group",
        "gender",
        "sex",
        "race",
        "ethnicity",
        "race_ethnicity",
        "education",
        "income",
        "party",
        "party_id",
        "political_party",
        "ideology",
        "political_ideology",
        "religion",
        "religious_affiliation",
        "region",
        "census_region",
        "state",
        "marital_status",
        "employment",
        "urbanicity",
        "generation",
        "citizenship",
    }
)

SOURCE_UNAVAILABLE = "unavailable"
"""``RouteDecision.source`` when routing.json could not be loaded at all."""

HasCredentialFn = Callable[[str], bool]


@dataclass(frozen=True)
class RouteDecision:
    """The outcome of :func:`route_models`.

    Exactly one of :attr:`model` (one model) or :attr:`model_spec` (a
    weighted blend of two or more models) is set.
    """

    model: str | None
    model_spec: list[tuple[str, float]] | None
    slice: str
    topic: str
    confident: bool
    source: str
    reason: str
    route_topic: str | None = None
    """Topic of the route actually used (``all`` after a fallback), or
    ``None`` when the default model was used."""
    fallback: str = "none"
    """``none`` (the requested route was used), ``slice-all`` (the slice's
    confident all-topics route was used), ``slice-top`` (the top candidate
    of the slice's all-topics route, without a clear lead) or ``default``
    (the default model was used)."""
    config_id: str | None = None
    label: str | None = None
    index: float | None = None
    n_questions: int | None = None
    dropped: list[str] = field(default_factory=list)
    """Members dropped because they cannot run here, as ``"model_id (why)"``."""
    slice_reason: str = ""
    topic_reason: str = ""

    @property
    def is_blend(self) -> bool:
        return bool(self.model_spec) and len(self.model_spec or []) > 1

    @property
    def primary_model(self) -> str:
        """The single model, or the highest-weight blend member."""
        if self.model is not None:
            return self.model
        assert self.model_spec
        return self.model_spec[0][0]

    def models_spec_string(self) -> str:
        """The blend as a CLI ``--models`` weighted spec (``a:0.5,b:0.5``)."""
        spec = self.model_spec or [(self.primary_model, 1.0)]
        return ",".join(f"{m}:{round(w, 6)}" for m, w in spec)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "model_spec": [{"model": m, "weight": round(w, 6)} for m, w in self.model_spec]
            if self.model_spec
            else None,
            "slice": self.slice,
            "topic": self.topic,
            "route_topic": self.route_topic,
            "confident": self.confident,
            "source": self.source,
            "fallback": self.fallback,
            "config_id": self.config_id,
            "label": self.label,
            "index": self.index,
            "n_questions": self.n_questions,
            "dropped": list(self.dropped),
            "reason": self.reason,
            "slice_reason": self.slice_reason,
            "topic_reason": self.topic_reason,
        }

    def format_lines(self) -> list[str]:
        """Human-readable lines for stderr."""
        if self.model_spec:
            models = ", ".join(f"{m} ({w:.0%})" for m, w in self.model_spec)
        else:
            models = str(self.model)
        return [
            f"model routing: {models}",
            (
                f"model routing: slice={self.slice} topic={self.topic} "
                f"confident={str(self.confident).lower()} source={self.source}"
            ),
            f"model routing: {self.reason}",
        ]


def is_auto_model(model: Any) -> bool:
    """True when *model* is the ``auto`` sentinel (case-insensitive)."""
    return isinstance(model, str) and model.strip().lower() == AUTO_MODEL


# ---------------------------------------------------------------------------
# Slice inference
# ---------------------------------------------------------------------------


def _normalize_population(population: Any) -> dict[str, list[str]]:
    if population is None:
        return {}
    if isinstance(population, str):
        if not population.strip():
            return {}
        from althing.population import parse_population

        return parse_population(population)
    if isinstance(population, dict):
        out: dict[str, list[str]] = {}
        for key, value in population.items():
            if isinstance(value, (list, tuple, set)):
                out[str(key)] = [str(v) for v in value]
            elif value is not None:
                out[str(key)] = [str(value)]
        return out
    raise ValueError(f"population must be a 'KEY=VALUE' string or a dict, got {type(population).__name__}")


def _pack_name(persona_pack: str | None) -> str | None:
    if not persona_pack:
        return None
    name = str(persona_pack).replace("\\", "/").rsplit("/", 1)[-1]
    for suffix in (".yaml", ".yml", ".json"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
    return name.strip().lower() or None


def infer_slice(population: Any = None, persona_pack: str | None = None) -> tuple[str, str]:
    """Return ``(slice_id, why)`` for a population spec and persona pack."""
    pop = _normalize_population(population)
    pack = _pack_name(persona_pack)
    countries = [v for k, vals in pop.items() if k.strip().lower() in _COUNTRY_KEYS for v in vals]
    non_us = [c for c in countries if c.strip().lower() not in _US_COUNTRY_VALUES]
    if non_us:
        return SLICE_CROSS_NATIONAL, f"population includes countries outside the US ({', '.join(non_us[:3])})"
    if pack in CROSS_NATIONAL_PACKS:
        return SLICE_CROSS_NATIONAL, f"persona pack {pack} represents respondents outside the US"
    demographic = sorted(k for k in pop if k.strip().lower() in US_DEMOGRAPHIC_KEYS)
    if demographic:
        return SLICE_US_SUBGROUPS, f"population filters on US demographic attributes ({', '.join(demographic)})"
    if pack in US_SUBGROUP_PACKS:
        return SLICE_US_SUBGROUPS, f"persona pack {pack} represents US demographic subgroups"
    return SLICE_US_GENERAL, "no population or pack signal, so the US general public slice is used"


# ---------------------------------------------------------------------------
# Topic classification
# ---------------------------------------------------------------------------


def _keyword_pattern(keyword: str) -> re.Pattern[str] | None:
    kw = keyword.strip().lower()
    if not kw:
        return None
    body = r"\s+".join(re.escape(part) for part in kw.split())
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def _taxonomy_topics(routing: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(routing, dict):
        return []
    taxonomy = routing.get("taxonomy")
    topics = taxonomy.get("topics") if isinstance(taxonomy, dict) else None
    if not isinstance(topics, list):
        return []
    return [t for t in topics if isinstance(t, dict) and isinstance(t.get("id"), str)]


def _question_texts(questions: Iterable[Any] | None) -> list[str]:
    texts: list[str] = []
    for q in questions or []:
        if isinstance(q, str):
            text = q
        elif isinstance(q, dict):
            text = q.get("text") if isinstance(q.get("text"), str) else ""
        else:
            text = str(getattr(q, "text", "") or "")
        if text and text.strip():
            texts.append(text)
    return texts


def classify_question(text: str, topics: list[dict[str, Any]]) -> str | None:
    """Return the taxonomy topic whose keywords best match *text*, or ``None``.

    The score is the number of distinct keywords found (whole-word match,
    never substrings). The highest score wins; a tie goes to the topic
    listed first in the taxonomy, which is how SynthBench's own keyword
    fallback breaks ties. No match at all gives ``None``.
    """
    best_id: str | None = None
    best_hits = 0
    for topic in topics:
        keywords = topic.get("keywords") if isinstance(topic.get("keywords"), list) else []
        hits = 0
        for kw in keywords:
            if not isinstance(kw, str):
                continue
            pat = _keyword_pattern(kw)
            if pat is not None and pat.search(text):
                hits += 1
        if hits > best_hits:
            best_id, best_hits = topic["id"], hits
    return best_id


def infer_topic(questions: Iterable[Any] | None, routing: dict[str, Any] | None) -> tuple[str, str]:
    """Return ``(topic_id, why)`` by majority over the question texts.

    A topic is used only when more than half of all questions classify to
    it; otherwise the result is ``all``.
    """
    texts = _question_texts(questions)
    topics = _taxonomy_topics(routing)
    if not texts:
        return TOPIC_ALL, "no question text to classify"
    if not topics:
        return TOPIC_ALL, "no topic taxonomy available"
    counts = Counter(t for t in (classify_question(x, topics) for x in texts) if t is not None)
    if not counts:
        return TOPIC_ALL, f"none of the {len(texts)} question(s) matched a topic keyword"
    topic, n = counts.most_common(1)[0]
    if n * 2 > len(texts):
        return topic, f"{n} of {len(texts)} question(s) match {topic} keywords"
    return TOPIC_ALL, f"no topic covers more than half of the {len(texts)} question(s)"


# ---------------------------------------------------------------------------
# Reachability
# ---------------------------------------------------------------------------


def _credential_env_for(model: str) -> tuple[str, ...] | None:
    """Credential env vars the provider for *model* needs, or ``None`` when
    it needs none we can check (local / subscription CLI / unknown)."""
    from althing.llm.aliases import resolve_alias

    if model.startswith(("ollama:", "local:", "claude-code:", "codex:")):
        return None
    canonical = resolve_alias(model)
    if canonical.startswith("openrouter/"):
        return ("OPENROUTER_API_KEY",)
    if canonical.startswith("claude-"):
        return ("ANTHROPIC_API_KEY",)
    if canonical.startswith("gemini-"):
        return ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    if canonical.startswith("grok-"):
        return ("XAI_API_KEY",)
    if canonical.startswith(("gpt-", "o1", "o3", "o4")):
        return ("OPENAI_API_KEY",)
    return None


def model_reachable(model: str, has_credential: HasCredentialFn | None = None) -> tuple[bool, str | None]:
    """Return ``(reachable, missing_env)`` from credentials alone (no network).

    The CLI still runs its network pre-flight on multi-model runs; this is
    the cheap check that keeps routing from picking a model the user has no
    key for.
    """
    envs = _credential_env_for(model)
    if envs is None:
        return True, None
    check = has_credential or _default_has_credential
    if any(check(e) for e in envs):
        return True, None
    return False, envs[0]


def _default_has_credential(env_var: str) -> bool:
    from althing.credentials import has_credential

    return has_credential(env_var)


# ---------------------------------------------------------------------------
# Route selection
# ---------------------------------------------------------------------------


def _find_route(routing: dict[str, Any], slice_id: str, topic: str) -> dict[str, Any] | None:
    routes = routing.get("routes")
    if not isinstance(routes, list):
        return None
    for route in routes:
        if isinstance(route, dict) and route.get("slice") == slice_id and route.get("topic") == topic:
            return route
    return None


def _find_candidate(route: dict[str, Any], config_id: Any) -> dict[str, Any] | None:
    if not isinstance(config_id, str):
        return None
    for cand in route.get("candidates") or []:
        if isinstance(cand, dict) and cand.get("config_id") == config_id:
            return cand
    return None


def _usable_members(
    candidate: dict[str, Any],
    *,
    max_members: int,
    has_credential: HasCredentialFn | None,
) -> tuple[list[tuple[str, float]], list[str]]:
    """Map candidate members to runnable models; drop the ones that cannot run.

    Returns ``(spec, dropped)`` with weights renormalized to sum to 1 and
    duplicate models merged. At most ``max_members`` members are kept (the
    highest-weight ones, ties in listed order).
    """
    raw_members = candidate.get("members")
    members = raw_members if isinstance(raw_members, list) else []
    weighted: list[tuple[str, float]] = []
    dropped: list[str] = []
    for item in members:
        if not isinstance(item, dict):
            continue
        model_id = item.get("model_id")
        w = item.get("weight")
        weight = float(w) if isinstance(w, (int, float)) and not isinstance(w, bool) and w > 0 else 1.0
        mapped = synthbench.runnable_model_for(model_id, has_credential=has_credential)
        if mapped is None:
            dropped.append(f"{model_id} (not a runnable model id)")
            continue
        ok, missing = model_reachable(mapped, has_credential)
        if not ok:
            dropped.append(f"{mapped} (no {missing})")
            continue
        weighted.append((mapped, weight))

    merged: dict[str, float] = {}
    for model, weight in weighted:
        merged[model] = merged.get(model, 0.0) + weight
    ordered = sorted(merged.items(), key=lambda kv: -kv[1])  # stable: ties keep listed order
    if max_members > 0 and len(ordered) > max_members:
        for model, _w in ordered[max_members:]:
            dropped.append(f"{model} (over the {max_members}-member limit)")
        ordered = ordered[:max_members]
    total = sum(w for _, w in ordered)
    if total <= 0:
        return [], dropped
    return [(m, w / total) for m, w in ordered], dropped


def _slice_label(routing: dict[str, Any] | None, slice_id: str) -> str:
    if isinstance(routing, dict):
        for s in routing.get("slices") or []:
            if isinstance(s, dict) and s.get("id") == slice_id and isinstance(s.get("label"), str):
                return s["label"]
    return DEFAULT_SLICES.get(slice_id, slice_id)


def _topic_label(routing: dict[str, Any] | None, topic: str) -> str:
    if topic == TOPIC_ALL:
        return "all topics"
    for t in _taxonomy_topics(routing):
        if t.get("id") == topic and isinstance(t.get("label"), str):
            return t["label"]
    return DEFAULT_TOPICS.get(topic, topic)


def _valid_slices(routing: dict[str, Any] | None) -> list[str]:
    ids = []
    if isinstance(routing, dict):
        ids = [s["id"] for s in routing.get("slices") or [] if isinstance(s, dict) and isinstance(s.get("id"), str)]
    return ids or list(DEFAULT_SLICES)


def _valid_topics(routing: dict[str, Any] | None) -> list[str]:
    ids = [t["id"] for t in _taxonomy_topics(routing)]
    return ids or list(DEFAULT_TOPICS)


def _route_problem(route: dict[str, Any] | None, slice_label: str, topic_label: str) -> str:
    if route is None:
        return f"SynthBench has no route for {slice_label} on {topic_label}"
    n = route.get("n_questions")
    n_part = f" (n={n})" if isinstance(n, int) else ""
    name = "all-topics" if topic_label == "all topics" else topic_label
    return f"the {name} route for {slice_label} is not confident{n_part}"


def route_models(
    *,
    questions: Iterable[Any] | None = None,
    population: Any = None,
    persona_pack: str | None = None,
    use_case: str | None = None,
    topic: str | None = None,
    allow_blend: bool = True,
    max_members: int = 4,
    default_model: str | None = None,
    routing: dict[str, Any] | None = None,
    source: str | None = None,
    has_credential: HasCredentialFn | None = None,
    client: Any = None,
    warn: Callable[[str], None] | None = None,
) -> RouteDecision:
    """Pick the model (or weighted blend) for a panel from SynthBench data.

    Args:
        questions: Question texts (strings or ``{"text": ...}`` dicts) used
            for topic inference.
        population: ``--population`` spec string or dict, used for slice
            inference.
        persona_pack: Persona pack name or path, used for slice inference.
        use_case: Explicit slice id; overrides inference.
        topic: Explicit topic id (or ``all``); overrides inference.
        allow_blend: When ``False``, use ``best_single_althing`` so the
            result is always one model.
        max_members: Cap on blend members (highest weights kept).
        default_model: Model used when there is no routing data for the
            slice or none of the chosen members can run. Callers
            pass their own surface default (CLI / MCP / SDK).
        routing: A pre-loaded routing.json document (tests / callers that
            already hold one). Loaded via :func:`synthbench.load_routing`
            when omitted.
        source: Provenance label for a pre-loaded ``routing``.
        has_credential: Credential check override (tests).

    Raises:
        ValueError: ``use_case`` or ``topic`` is not a known id.
    """
    if routing is None:
        loaded = synthbench.load_routing(client=client, warn=warn)
        if loaded is not None:
            routing = loaded.leaderboard
            source = loaded.source
        else:
            source = SOURCE_UNAVAILABLE
    source = source or "live"

    fallback_model = default_model or "sonnet"

    # Slice
    if use_case is not None and str(use_case).strip():
        slice_id = str(use_case).strip()
        valid = _valid_slices(routing)
        if slice_id not in valid:
            raise ValueError(f"unknown use case {slice_id!r}; expected one of: {', '.join(valid)}")
        slice_reason = "use case given explicitly"
    else:
        slice_id, slice_reason = infer_slice(population, persona_pack)

    # Topic
    if topic is not None and str(topic).strip():
        topic_id = str(topic).strip()
        valid_topics = _valid_topics(routing)
        if topic_id != TOPIC_ALL and topic_id not in valid_topics:
            raise ValueError(f"unknown topic {topic_id!r}; expected one of: {', '.join([TOPIC_ALL, *valid_topics])}")
        topic_reason = "topic given explicitly"
    else:
        topic_id, topic_reason = infer_topic(questions, routing)

    slice_label = _slice_label(routing, slice_id)

    def _default(why: str, dropped: list[str] | None = None) -> RouteDecision:
        return RouteDecision(
            model=fallback_model,
            model_spec=None,
            slice=slice_id,
            topic=topic_id,
            confident=False,
            source=source or SOURCE_UNAVAILABLE,
            reason=f"{_cap(why)}, so this run uses the default model {fallback_model}.",
            route_topic=None,
            fallback="default",
            dropped=list(dropped or []),
            slice_reason=slice_reason,
            topic_reason=topic_reason,
        )

    if not isinstance(routing, dict):
        return _default("SynthBench routing data is unavailable")

    # (fallback label, route topic, route must be confident). The last level
    # takes the slice's top scorer even without a clear lead: it still beats
    # a default model that SynthBench ranks lower or never measured.
    levels: list[tuple[str, str, bool]] = []
    if topic_id != TOPIC_ALL:
        levels.append(("none", topic_id, True))
    levels.append(("slice-all" if topic_id != TOPIC_ALL else "none", TOPIC_ALL, True))
    levels.append(("slice-top", TOPIC_ALL, False))

    problems: list[str] = []
    all_dropped: list[str] = []
    for fallback, route_topic, need_confident in levels:
        topic_label = _topic_label(routing, route_topic)
        route = _find_route(routing, slice_id, route_topic)
        if route is None:
            if need_confident:
                problems.append(_route_problem(route, slice_label, topic_label))
            continue
        confident = route.get("confident") is True
        if need_confident and not confident:
            problems.append(_route_problem(route, slice_label, topic_label))
            continue
        if not need_confident and confident:
            continue  # already tried as a confident route above
        pick_key = "best_althing" if allow_blend else "best_single_althing"
        candidate = _find_candidate(route, route.get(pick_key))
        if candidate is None:
            problems.append(f"the route for {slice_label} on {topic_label} has no Althing-conditioned candidate")
            continue
        spec, dropped = _usable_members(candidate, max_members=max_members, has_credential=has_credential)
        all_dropped.extend(dropped)
        if not spec:
            problems.append(
                f"none of the members of {candidate.get('label') or candidate.get('config_id')} can run here"
            )
            continue

        label = str(candidate.get("label") or candidate.get("config_id"))
        index = candidate.get("index")
        index_f = float(index) if isinstance(index, (int, float)) and not isinstance(index, bool) else None
        n_q = route.get("n_questions") if isinstance(route.get("n_questions"), int) else None
        evidence = []
        if index_f is not None:
            evidence.append(f"SynthBench index {index_f:.1f}")
        if n_q is not None:
            evidence.append(f"{n_q} questions")
        evidence_part = f" ({', '.join(evidence)})" if evidence else ""
        kind = "blend" if len(spec) > 1 else "model"
        why = f"{problems[0]} and {problems[-1]}" if len(problems) > 1 else (problems[0] if problems else "")
        lead = f"{_cap(why)}, so " if why else ""
        pick = (
            f"{label} is the top Althing-conditioned {kind} for {slice_label} on {topic_label}{evidence_part}"
            if not problems
            else f"this uses {label}, the top Althing-conditioned {kind} for {slice_label} on all topics{evidence_part}"
        )
        if not confident:
            pick += ", though its lead over the next system is not clear"
        drop_part = f"; dropped {', '.join(dropped)}" if dropped else ""
        reason = f"{lead}{pick}{drop_part}."
        return RouteDecision(
            model=spec[0][0] if len(spec) == 1 else None,
            model_spec=spec if len(spec) > 1 else None,
            slice=slice_id,
            topic=topic_id,
            confident=confident,
            source=source,
            reason=_cap(reason),
            route_topic=route_topic,
            fallback=fallback,
            config_id=str(candidate.get("config_id")) if candidate.get("config_id") else None,
            label=label,
            index=index_f,
            n_questions=n_q,
            dropped=dropped,
            slice_reason=slice_reason,
            topic_reason=topic_reason,
        )

    why = problems[-1] if problems else "no SynthBench route applies"
    if len(problems) > 1:
        why = f"{problems[0]} and {problems[-1]}"
    return _default(why, all_dropped)


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


# ---------------------------------------------------------------------------
# Applying a decision
# ---------------------------------------------------------------------------


def apply_decision_to_personas(
    decision: RouteDecision,
    personas: list[dict[str, Any]],
) -> tuple[str, dict[str, str] | None, list[str]]:
    """Turn a decision into ``(model, persona_models, warnings)``.

    A single-model decision returns ``(model, None, [])``. A blend is split
    across personas with the same largest-remainder split the CLI uses for
    ``--models a:0.5,b:0.5``; per-persona ``model`` overrides in the persona
    data still win. ``model`` is the highest-weight member (used for cost
    fallbacks and synthesis defaults).
    """
    if not decision.model_spec:
        return decision.primary_model, None, []
    from althing.model_split import assign_models_to_personas

    persona_models, warnings = assign_models_to_personas(personas, decision.model_spec, decision.primary_model)
    return decision.primary_model, persona_models, warnings


__all__ = [
    "AUTO_MODEL",
    "DEFAULT_SLICES",
    "DEFAULT_TOPICS",
    "SLICE_CROSS_NATIONAL",
    "SLICE_US_GENERAL",
    "SLICE_US_SUBGROUPS",
    "SOURCE_UNAVAILABLE",
    "TOPIC_ALL",
    "RouteDecision",
    "apply_decision_to_personas",
    "classify_question",
    "infer_slice",
    "infer_topic",
    "is_auto_model",
    "model_reachable",
    "route_models",
]
