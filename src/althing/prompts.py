"""Shared prompt builders for persona system prompts and question formatting."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jinja2
from jinja2.sandbox import SandboxedEnvironment

from althing.llm.models import (
    ContentBlock,
    DocumentBlock,
    FileRefSource,
    HTMLBlock,
    ImageBlock,
    InlineSource,
    TextBlock,
    URLBlock,
    URLSource,
)

if TYPE_CHECKING:
    pass

# Synthesis prompt version — increment when the prompt changes materially.
SYNTHESIS_PROMPT_VERSION = 1

SYNTHESIS_PROMPT = (
    "You are a research analyst synthesizing responses from a focus group panel. "
    "Multiple panelists answered the same set of questions. Your job is to identify "
    "patterns, agreements, disagreements, and surprising insights across their responses.\n\n"
    "Analyze the panelist responses below and produce a structured synthesis with:\n"
    "- summary: A concise overview of the key findings (2-4 sentences)\n"
    "- themes: The main themes that emerged across responses\n"
    "- agreements: Points where panelists broadly agreed\n"
    "- disagreements: Points where panelists diverged or disagreed\n"
    "- surprises: Unexpected or notable findings\n"
    "- recommendation: A brief actionable recommendation based on the findings\n\n"
    "Be specific and cite panelist names when relevant. Focus on substance, not meta-commentary."
)

# Markers that distinguish Jinja2 templates from legacy Python format strings.
_JINJA2_MARKERS = ("{{", "{%", "{#")


def is_jinja2_template(template: str) -> bool:
    """Return True if the string contains Jinja2 syntax markers."""
    return any(marker in template for marker in _JINJA2_MARKERS)


def compile_jinja2_template(template_str: str) -> jinja2.Template:
    """Compile a Jinja2 template string using SandboxedEnvironment.

    Validates syntax at compile time — callers should do this once at
    load time, not per persona render.  Raises
    :class:`jinja2.TemplateSyntaxError` on invalid syntax.
    """
    env = SandboxedEnvironment()
    return env.from_string(template_str)


def persona_system_prompt(persona: dict[str, Any]) -> str:
    """Build a system prompt from a persona definition."""
    parts = [f"You are role-playing as {persona.get('name', 'an anonymous person')}."]
    if persona.get("age"):
        parts.append(f"Age: {persona['age']}.")
    # Demographic attributes used by population packs (althing.population).
    for key in ("gender", "country", "region"):
        if persona.get(key):
            parts.append(f"{key.capitalize()}: {persona[key]}.")
    if persona.get("occupation"):
        parts.append(f"Occupation: {persona['occupation']}.")
    if persona.get("background"):
        parts.append(f"Background: {persona['background']}.")
    if persona.get("personality_traits"):
        traits = persona["personality_traits"]
        if isinstance(traits, list):
            traits = ", ".join(str(t) for t in traits)
        parts.append(f"Personality traits: {traits}.")
    parts.append(
        "Answer questions in character. Be authentic to this persona's "
        "perspective, experiences, and communication style. "
        "Give concise, direct answers."
    )
    return " ".join(parts)


def load_prompt_template(path: str) -> str:
    """Load a prompt template from a file path.

    Supports both Jinja2 (``{{ name }}``, ``{% if ... %}``) and legacy
    Python format-string (``{name}``) syntax — detected automatically by
    :func:`is_jinja2_template`.  Returns the raw template string; callers
    should call :func:`compile_jinja2_template` if Jinja2 is detected so
    validation happens at load time rather than per render.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return p.read_text(encoding="utf-8")


class _DefaultDict(defaultdict):
    """A defaultdict that returns '{key}' for missing keys during format_map."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def persona_system_prompt_from_template(
    persona: dict[str, Any],
    template: str | jinja2.Template,
) -> str:
    """Build a system prompt by applying a template to the persona dict.

    Accepts either:

    - A pre-compiled :class:`jinja2.Template` (preferred — compile once
      via :func:`compile_jinja2_template` at load time for best performance).
    - A raw ``str`` with Jinja2 syntax (``{{ name }}``, ``{% if ... %}``
      etc.) — auto-detected and compiled inline on each call.
    - A raw ``str`` with legacy Python ``{name}``-style placeholders —
      rendered via ``str.format_map`` with a tolerant dict that leaves
      unknown keys as literal ``{key}`` strings.

    The ``personality_traits`` field is pre-joined with ``", "`` when using
    the legacy format path.  Jinja2 templates receive the raw list and can
    use ``{{ personality_traits | join(", ") }}`` for the same effect.
    """
    if isinstance(template, jinja2.Template):
        return template.render(**persona)
    if is_jinja2_template(template):
        return compile_jinja2_template(template).render(**persona)
    ctx = dict(persona)
    traits = ctx.get("personality_traits")
    if isinstance(traits, list):
        ctx["personality_traits"] = ", ".join(str(t) for t in traits)
    return template.format_map(_DefaultDict(str, ctx))


def build_question_prompt(question: dict[str, Any]) -> str:
    """Build a user prompt from a question definition.

    Returns the question text only — multimodal block emission lives in
    :func:`build_question_blocks` (hq-0pbp). This stays the legacy text-only
    builder for size estimation, previews, and call paths that don't carry
    attachments.
    """
    text = question.get("text", question) if isinstance(question, dict) else str(question)
    return str(text)


# ---------------------------------------------------------------------------
# Multimodal question prompt blocks (hq-0pbp / D-phase hq-cxth)
# ---------------------------------------------------------------------------


_DEFAULT_IMAGE_MEDIA_TYPE = "image/png"
_DEFAULT_DOCUMENT_MEDIA_TYPE = "application/pdf"
_VALID_IMAGE_MEDIA_TYPES: frozenset[str] = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def _attachment_source(att: dict[str, Any]) -> Any:
    """Build the typed source variant from an attachment dict.

    Accepts the three source shapes validated by
    :mod:`althing.instrument`: inline base64, public URL, or
    Files-API ref. Raises :class:`ValueError` on unrecognized shapes so
    bad attachment dicts surface here instead of at wire serialization.
    """
    src = att.get("source")
    if not isinstance(src, dict):
        raise ValueError(f"attachment {att.get('type')!r} requires a 'source' mapping")
    stype = src.get("type")
    if stype == "base64":
        data = src.get("data", "")
        return InlineSource(data=data)
    if stype == "url":
        return URLSource(url=src.get("url", ""))
    if stype == "file":
        return FileRefSource(file_id=src.get("file_id", ""))
    raise ValueError(f"unrecognized attachment source.type: {stype!r}")


def _attachment_to_block(att: dict[str, Any]) -> ContentBlock:
    """Convert a single attachment dict to a typed :class:`ContentBlock`.

    The dict shape matches what
    :func:`althing.instrument._validate_attachment` accepts: ``type``
    discriminator + ``source`` (or ``url``/``text`` for ``url``/``html``),
    optional ``media_type``. Unknown types raise ``ValueError`` rather than
    silently dropping content — the orchestrator catches and records the
    failure as a per-question error.
    """
    atype = att.get("type")
    if atype == "image":
        media_type = att.get("media_type", _DEFAULT_IMAGE_MEDIA_TYPE)
        if media_type not in _VALID_IMAGE_MEDIA_TYPES:
            raise ValueError(f"image attachment media_type {media_type!r} not supported")
        return ImageBlock(source=_attachment_source(att), media_type=media_type)
    if atype == "document":
        media_type = att.get("media_type", _DEFAULT_DOCUMENT_MEDIA_TYPE)
        return DocumentBlock(source=_attachment_source(att), media_type=media_type)
    if atype == "html":
        text = att.get("text", "")
        if not isinstance(text, str):
            raise ValueError("html attachment requires a 'text' string")
        return HTMLBlock(text=text)
    if atype == "url":
        url = att.get("url", "")
        if not isinstance(url, str) or not url:
            raise ValueError("url attachment requires a non-empty 'url' string")
        fetch_mode = att.get("fetch_mode", "auto")
        return URLBlock(url=url, fetch_mode=fetch_mode)
    raise ValueError(f"unsupported attachment type for block emission: {atype!r}")


def _apply_cache_marker(block: ContentBlock) -> ContentBlock:
    """Return a copy of ``block`` with ``cache_control='ephemeral'`` set.

    Only the four block types that carry a ``cache_control`` field are
    supported (Image, Document, HTML, Text). Tool-use / tool-result
    blocks are returned unchanged — the marker only makes sense on
    user-facing content.
    """
    if isinstance(block, ImageBlock):
        return ImageBlock(source=block.source, media_type=block.media_type, cache_control="ephemeral")
    if isinstance(block, DocumentBlock):
        return DocumentBlock(source=block.source, media_type=block.media_type, cache_control="ephemeral")
    if isinstance(block, HTMLBlock):
        return HTMLBlock(text=block.text, cache_control="ephemeral")
    if isinstance(block, TextBlock):
        # TextBlock has no cache_control field; the Anthropic provider
        # adds the marker at wire time when ``cache_enabled`` is set.
        return block
    return block


def build_question_blocks(
    question: dict[str, Any],
    attachments: list[dict[str, Any]] | None = None,
    *,
    panel_shared_attachments: list[dict[str, Any]] | None = None,
    cache_marker: bool = False,
) -> list[ContentBlock]:
    """Emit the per-question user-message content blocks (hq-0pbp).

    Anthropic-cited canonical order (Vision + PDF docs: image-then-text):

        1. Panel-shared documents
        2. Panel-shared images
        3. Per-question attachments (already filtered per persona by
           :func:`althing.attachments.filter_attachments`)
        4. Question text

    When ``cache_marker`` is True, the prefix-cache breakpoint is placed
    on the LAST block before the question text — i.e., the last attachment
    if any are present, otherwise the question text itself (delegated to
    the provider's auto-marker via ``request.cache_enabled``). Caller is
    responsible for the P≥2 / 1024-token-prefix predicate; this helper
    just applies the marker it's told to.

    HTML attachments (hq-aaca): emitting the HTML payload as a separate
    text content block alongside the question text caused ~50% refusal
    rates via OpenRouter (model treated one of the two text blocks as
    tangential). Per midgard's empirically-working pattern (0/210
    refusals) we inline the HTML *into* the question text wrapped in a
    delimiter, so the user message ships exactly one text block with the
    HTML embedded as user-supplied data.
    """
    blocks: list[ContentBlock] = []
    html_payloads: list[str] = []
    shared = list(panel_shared_attachments or [])
    per_q = list(attachments or [])

    def _emit(att: dict[str, Any]) -> None:
        if att.get("type") == "html":
            html_text = att.get("text", "")
            if not isinstance(html_text, str):
                raise ValueError("html attachment requires a 'text' string")
            if html_text:
                html_payloads.append(html_text)
            return
        blocks.append(_attachment_to_block(att))

    # 1+2: panel-shared docs first, then panel-shared images
    shared_docs = [a for a in shared if a.get("type") == "document"]
    shared_images = [a for a in shared if a.get("type") == "image"]
    shared_other = [a for a in shared if a.get("type") not in ("document", "image")]
    for att in (*shared_docs, *shared_images, *shared_other):
        _emit(att)

    # 3: per-question attachments in caller-supplied order
    for att in per_q:
        _emit(att)

    # 4: question text — with any HTML attachments inlined as a
    #    delimited prefix so the wire ships a single text block.
    text = question.get("text", question) if isinstance(question, dict) else str(question)
    raw_text = str(text)
    if html_payloads:
        prefix = "\n\n".join(f"--- HTML SOURCE ---\n{html}\n--- END HTML SOURCE ---" for html in html_payloads)
        text_payload = f"{prefix}\n\n{raw_text}"
    else:
        text_payload = raw_text
    text_block = TextBlock(text=text_payload)

    if cache_marker and blocks:
        # Mark the LAST attachment block (the cache breakpoint sits at the
        # end of the cacheable prefix; the question text trails it).
        blocks[-1] = _apply_cache_marker(blocks[-1])
    blocks.append(text_block)
    return blocks
