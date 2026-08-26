"""LaTeX parser.

LaTeX is the easiest of the three formats and the most valuable: structure is
explicit, so ``\\section`` really is a section and ``\\begin{figure}`` really is a
figure. No reconstruction is needed, which means offsets and provenance are exact
rather than inferred.

Design decision worth stating: **maths is preserved verbatim, never stripped.**
``$\\mathcal{R}(h)$`` stays as LaTeX so the Beamer renderer can typeset it
natively and the PPTX renderer can at least show the author's own notation. A
parser that flattened it to "R(h)" would silently destroy the content that
matters most in a theory talk.
"""

from __future__ import annotations

import os
import re

from rostrum.ingest.model import (
    ExtractedAsset,
    ParsedDocument,
    SegmentKind,
    TextBuilder,
)
from rostrum.ir.enums import AssetKind

# Sectioning commands, in depth order.
_SECTION_LEVELS = {
    "part": 1,
    "chapter": 1,
    "section": 1,
    "subsection": 2,
    "subsubsection": 3,
    "paragraph": 4,
}

_ENV_RE = re.compile(
    r"\\begin\{(?P<env>[a-zA-Z*]+)\}(?P<body>.*?)\\end\{(?P=env)\}", re.DOTALL
)
_SECTION_RE = re.compile(
    r"\\(?P<cmd>part|chapter|section|subsection|subsubsection|paragraph)\*?\s*"
    r"(?:\[[^\]]*\])?\s*\{",
)
_COMMENT_RE = re.compile(r"(?<!\\)%.*?$", re.MULTILINE)
_LABEL_RE = re.compile(r"\\label\{([^}]*)\}")
_CAPTION_RE = re.compile(r"\\caption\*?\s*\{")
_GRAPHIC_RE = re.compile(
    r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}"
)
_INPUT_RE = re.compile(r"\\(?:input|include)\s*\{([^}]*)\}")

_LIST_ENVS = {"itemize", "enumerate", "description"}
_MATH_ENVS = {
    "equation", "equation*", "align", "align*", "gather", "gather*",
    "multline", "multline*", "eqnarray", "eqnarray*", "displaymath",
}
_FLOAT_ENVS = {"figure", "figure*", "table", "table*", "wrapfigure"}
_VERBATIM_ENVS = {"verbatim", "lstlisting", "minted", "Verbatim"}
_SKIP_ENVS = {"thebibliography", "abstract"} | _VERBATIM_ENVS

# Commands whose argument is content and should be unwrapped, not dropped.
_UNWRAP = (
    "textbf", "textit", "emph", "texttt", "textsc", "underline",
    "textrm", "textsf", "mbox", "text", "bm", "boldsymbol",
)
# Commands to delete entirely, argument included.
_DROP = ("label", "index", "footnote", "cite", "citep", "citet", "ref", "eqref",
         "vspace", "hspace", "centering", "noindent", "small", "footnotesize")


def parse_latex(
    path: str,
    *,
    doc_id: str = "manuscript",
    language: str = "zh",
    asset_dir: str | None = None,
    follow_inputs: bool = True,
) -> ParsedDocument:
    """Parse a ``.tex`` file into a :class:`ParsedDocument`."""
    root = os.path.dirname(os.path.abspath(path))
    with open(path, encoding="utf-8", errors="replace") as fh:
        source = fh.read()

    warnings: list[str] = []
    if follow_inputs:
        source = _inline_inputs(source, root, warnings)

    source = _COMMENT_RE.sub("", source)
    body = _document_body(source)

    builder = TextBuilder()
    assets: list[ExtractedAsset] = []
    section_stack: list[str] = []
    figure_n = 0
    table_n = 0

    for chunk in _split_top_level(body):
        if chunk["type"] == "section":
            level = _SECTION_LEVELS.get(chunk["cmd"], 2)
            title = _to_text(chunk["title"])
            chunk_label = chunk.get("label")
            if not title:
                continue
            del section_stack[level - 1 :]
            section_stack.append(title)
            builder.add(
                title,
                SegmentKind.HEADING,
                level=level,
                locator=chunk_label,
                section_path=tuple(section_stack[:-1]),
            )
            continue

        if chunk["type"] == "environment":
            env = chunk["env"]
            inner = chunk["body"]

            if env in _FLOAT_ENVS:
                is_table = env.startswith("table")
                caption = _extract_caption(inner)
                label = _first(_LABEL_RE, inner)
                if is_table:
                    table_n += 1
                    asset = _table_asset(inner, table_n, caption, label)
                else:
                    figure_n += 1
                    asset = _figure_asset(inner, figure_n, caption, label, root)
                if asset is None:
                    continue
                seg = builder.add(
                    caption or f"[{asset.source_label}]",
                    SegmentKind.CAPTION,
                    locator=label,
                    section_path=tuple(section_stack),
                    asset_id=asset.asset_id,
                )
                if seg is not None:
                    asset.spans.append(seg.span(doc_id))
                assets.append(asset)
                continue

            if env in _MATH_ENVS:
                # Keep the maths verbatim: it is the content, not decoration.
                # Only \label is stripped, and it becomes the locator instead --
                # leaving it inline would put markup on the rendered slide.
                label = _first(_LABEL_RE, inner)
                latex = _LABEL_RE.sub("", inner).strip()
                builder.add(
                    latex,
                    SegmentKind.EQUATION,
                    locator=label,
                    section_path=tuple(section_stack),
                )
                continue

            if env in _LIST_ENVS:
                for level, item in _items(inner):
                    text = _to_text(item)
                    if text:
                        builder.add(
                            text,
                            SegmentKind.LIST_ITEM,
                            level=level,
                            section_path=tuple(section_stack),
                        )
                continue

            if env in _VERBATIM_ENVS:
                builder.add(
                    inner.strip(),
                    SegmentKind.CODE,
                    section_path=tuple(section_stack),
                )
                continue

            if env in _SKIP_ENVS:
                continue

            # Unknown environment: treat its body as prose rather than losing it.
            for para in _paragraphs(inner):
                text = _to_text(para)
                if text:
                    builder.add(
                        text,
                        SegmentKind.PARAGRAPH,
                        section_path=tuple(section_stack),
                    )
            continue

        for para in _paragraphs(chunk["text"]):
            text = _to_text(para)
            if text:
                builder.add(
                    text,
                    SegmentKind.PARAGRAPH,
                    section_path=tuple(section_stack),
                )

    parsed = ParsedDocument(
        doc_id=doc_id,
        text=builder.text,
        segments=builder.segments,
        assets=assets,
        title=_to_text(_first(re.compile(r"\\title\s*\{"), source, braced=True) or "")
        or None,
        authors=_authors(source),
        source_path=path,
        source_format="latex",
        language=language,
        warnings=warnings,
    )
    parsed.warnings.extend(parsed.verify())
    return parsed


# --------------------------------------------------------------------------- #
# Structure walking
# --------------------------------------------------------------------------- #


def _document_body(source: str) -> str:
    match = re.search(
        r"\\begin\{document\}(.*?)(?:\\end\{document\}|\Z)", source, re.DOTALL
    )
    return match.group(1) if match else source


def _split_top_level(body: str) -> list[dict]:
    """Split the body into sections, environments and free text, in order."""
    out: list[dict] = []
    i = 0
    n = len(body)

    while i < n:
        sec = _SECTION_RE.search(body, i)
        env = _find_env(body, i)

        nxt = min(
            [p for p in (sec.start() if sec else None, env[0] if env else None)
             if p is not None],
            default=None,
        )
        if nxt is None:
            text = body[i:]
            if text.strip():
                out.append({"type": "text", "text": text})
            break

        if nxt > i:
            text = body[i:nxt]
            if text.strip():
                out.append({"type": "text", "text": text})

        if sec is not None and sec.start() == nxt:
            title, end = _read_braced(body, sec.end() - 1)
            # A \label immediately after the heading names this section; capture
            # it as the locator so a span can cite "sec:method" rather than a
            # page number.
            tail = body[end : end + 80]
            lbl = _LABEL_RE.search(tail)
            out.append(
                {
                    "type": "section",
                    "cmd": sec.group("cmd"),
                    "title": title,
                    "label": lbl.group(1) if lbl and tail[: lbl.start()].strip() == "" else None,
                }
            )
            i = end
            continue

        _, env_name, inner, end = env  # type: ignore[misc]
        out.append({"type": "environment", "env": env_name, "body": inner})
        i = end
    return out


def _find_env(body: str, start: int):
    """Find the next environment, matching nested begin/end correctly."""
    m = re.compile(r"\\begin\{([a-zA-Z*]+)\}").search(body, start)
    if m is None:
        return None
    name = m.group(1)
    depth = 1
    pos = m.end()
    begin_re = re.compile(rf"\\begin\{{{re.escape(name)}\}}")
    end_re = re.compile(rf"\\end\{{{re.escape(name)}\}}")
    while depth > 0:
        nb = begin_re.search(body, pos)
        ne = end_re.search(body, pos)
        if ne is None:
            return (m.start(), name, body[m.end():], len(body))
        if nb is not None and nb.start() < ne.start():
            depth += 1
            pos = nb.end()
            continue
        depth -= 1
        pos = ne.end()
        if depth == 0:
            return (m.start(), name, body[m.end() : ne.start()], ne.end())
    return None  # pragma: no cover


def _read_braced(text: str, brace_pos: int) -> tuple[str, int]:
    """Read a balanced ``{...}`` group starting at ``brace_pos``."""
    depth = 0
    i = brace_pos
    n = len(text)
    while i < n:
        c = text[i]
        if c == "{" and (i == 0 or text[i - 1] != "\\"):
            depth += 1
        elif c == "}" and text[i - 1] != "\\":
            depth -= 1
            if depth == 0:
                return text[brace_pos + 1 : i], i + 1
        i += 1
    return text[brace_pos + 1 :], n


def _items(inner: str) -> list[tuple[int, str]]:
    """Split a list environment into ``(level, text)`` items.

    Nested lists are flattened with their depth recorded, because the IR carries
    nesting as a level on a flat block sequence.
    """
    out: list[tuple[int, str]] = []

    def walk(text: str, level: int) -> None:
        nested = _find_env(text, 0)
        while nested and nested[1] not in _LIST_ENVS:
            nested = _find_env(text, nested[3])
        if nested and nested[1] in _LIST_ENVS:
            before, _, body, after_pos = nested
            walk_flat(text[:before], level)
            walk(body, level + 1)
            walk(text[after_pos:], level)
            return
        walk_flat(text, level)

    def walk_flat(text: str, level: int) -> None:
        for piece in re.split(r"\\item\b", text)[1:]:
            piece = re.sub(r"^\s*\[[^\]]*\]", "", piece)
            if piece.strip():
                out.append((min(level, 3), piece))

    walk(inner, 0)
    return out


def _paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


# --------------------------------------------------------------------------- #
# Assets
# --------------------------------------------------------------------------- #


def _figure_asset(
    inner: str, index: int, caption: str | None, label: str | None, root: str
) -> ExtractedAsset | None:
    graphics = _GRAPHIC_RE.findall(inner)
    path = None
    aspect = None
    if graphics:
        path = _resolve_graphic(graphics[0], root)
        aspect = _aspect(path) if path else None
    return ExtractedAsset(
        kind=AssetKind.FIGURE,
        asset_id=f"fig-{index:02d}",
        path=path,
        caption=caption,
        source_label=f"Figure {index}",
        intrinsic_aspect=aspect,
    )


def _resolve_graphic(name: str, root: str) -> str | None:
    """Resolve an ``\\includegraphics`` argument to a real file.

    LaTeX omits the extension, so the candidates must be tried in the order
    ``graphicx`` itself would.
    """
    candidates = [name]
    if not os.path.splitext(name)[1]:
        candidates = [
            f"{name}{ext}"
            for ext in (".pdf", ".png", ".jpg", ".jpeg", ".eps", ".svg")
        ]
    for rel in candidates:
        for base in (root, os.path.join(root, "figures"), os.path.join(root, "figs"),
                     os.path.join(root, "images"), os.path.join(root, "img")):
            candidate = os.path.join(base, rel)
            if os.path.exists(candidate):
                return candidate
    return None


def _aspect(path: str) -> float | None:
    try:
        from PIL import Image

        with Image.open(path) as im:
            w, h = im.size
        return (w / h) if h else None
    except Exception:
        return None


def _table_asset(
    inner: str, index: int, caption: str | None, label: str | None
) -> ExtractedAsset:
    data = _parse_tabular(inner)
    return ExtractedAsset(
        kind=AssetKind.TABLE,
        asset_id=f"tbl-{index:02d}",
        data=data,
        latex=inner.strip() if not data else None,
        caption=caption,
        source_label=f"Table {index}",
    )


def _parse_tabular(inner: str) -> dict | None:
    """Recover cell values from a ``tabular`` so the table can be re-rendered."""
    env = None
    for name in ("tabular", "tabularx", "longtable", "tabu"):
        found = _find_env_named(inner, name)
        if found:
            env = found
            break
    if env is None:
        return None

    body = env
    body = re.sub(r"\\(?:top|mid|bottom)rule|\\hline|\\cline\{[^}]*\}", "", body)
    body = re.sub(r"^\s*\{[^}]*\}", "", body, count=1)  # column spec

    rows: list[list[str]] = []
    for raw in re.split(r"\\\\", body):
        if not raw.strip():
            continue
        cells = [_to_text(c) for c in raw.split("&")]
        if any(cells):
            rows.append(cells)
    if not rows:
        return None

    columns: list[str] = []
    if len(rows) > 1 and not any(_numeric(c) for c in rows[0]):
        columns, rows = rows[0], rows[1:]
    return {"columns": columns, "rows": rows}


def _find_env_named(text: str, name: str) -> str | None:
    m = re.search(
        rf"\\begin\{{{name}\}}(?:\s*\[[^\]]*\])?\s*(?:\{{[^}}]*\}})?(.*?)"
        rf"\\end\{{{name}\}}",
        text,
        re.DOTALL,
    )
    return m.group(1) if m else None


def _numeric(cell: str) -> bool:
    return bool(re.fullmatch(r"[-+]?[\d.,]+%?", cell.strip()))


def _extract_caption(inner: str) -> str | None:
    m = _CAPTION_RE.search(inner)
    if m is None:
        return None
    body, _ = _read_braced(inner, m.end() - 1)
    return _to_text(body) or None


def _first(pattern: re.Pattern, text: str, *, braced: bool = False) -> str | None:
    m = pattern.search(text)
    if m is None:
        return None
    if braced:
        body, _ = _read_braced(text, m.end() - 1)
        return body
    return m.group(1)


def _authors(source: str) -> list[str]:
    raw = _first(re.compile(r"\\author\s*\{"), source, braced=True)
    if not raw:
        return []
    raw = re.sub(r"\\(?:thanks|inst|affiliation)\s*\{[^}]*\}", "", raw)
    parts = re.split(r"\\and\b|,|、|；|;", raw)
    return [t for t in (_to_text(p) for p in parts) if t]


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #


def _to_text(latex: str) -> str:
    """Reduce LaTeX markup to plain text, keeping inline maths verbatim.

    Inline maths is preserved with its delimiters so downstream renderers can
    typeset it. Everything else is unwrapped or dropped according to whether its
    argument is content.
    """
    if not latex:
        return ""
    out = latex

    # Protect inline maths from the command-stripping passes below.
    maths: list[str] = []

    def stash(m: re.Match) -> str:
        maths.append(m.group(0))
        return f"\x00{len(maths) - 1}\x00"

    out = re.sub(r"\$\$.+?\$\$|\$[^$]+\$|\\\(.+?\\\)", stash, out, flags=re.DOTALL)

    for cmd in _DROP:
        out = re.sub(rf"\\{cmd}\s*(?:\[[^\]]*\])?\s*\{{[^{{}}]*\}}", "", out)
        out = re.sub(rf"\\{cmd}\b", "", out)
    for cmd in _UNWRAP:
        out = re.sub(rf"\\{cmd}\s*\{{", "{", out)

    out = out.replace("\\\\", " ").replace("~", " ")
    out = re.sub(r"\\(?:newline|par|hfill|centering|item)\b", " ", out)
    out = re.sub(r"\\[a-zA-Z]+\*?\s*(?:\[[^\]]*\])?", "", out)
    out = out.replace("{", "").replace("}", "")
    out = re.sub(r"\\([&%$#_{}])", r"\1", out)
    out = re.sub(r"\s+", " ", out)

    for i, m in enumerate(maths):
        out = out.replace(f"\x00{i}\x00", m)
    return out.strip()


def _inline_inputs(source: str, root: str, warnings: list[str], depth: int = 0) -> str:
    """Splice ``\\input``/``\\include`` files in, so multi-file papers work."""
    if depth > 5:  # pragma: no cover - pathological nesting
        return source

    def replace(m: re.Match) -> str:
        name = m.group(1).strip()
        for candidate in (name, f"{name}.tex"):
            full = os.path.join(root, candidate)
            if os.path.exists(full):
                with open(full, encoding="utf-8", errors="replace") as fh:
                    return _inline_inputs(fh.read(), root, warnings, depth + 1)
        warnings.append(f"could not resolve \\input{{{name}}}")
        return ""

    return _INPUT_RE.sub(replace, source)
