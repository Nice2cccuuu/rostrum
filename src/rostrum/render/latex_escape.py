"""Escaping text for LaTeX.

Separated from the emitter because getting this wrong is both easy and
catastrophic: an unescaped ``%`` silently comments out the rest of a line, so a
bullet loses its ending and the slide looks fine in the source and wrong in the
PDF. That failure is quiet, which makes it worse than a compile error.

Two rules govern everything here.

**Escape by default, pass through only what was declared.** Manuscript prose is
escaped unconditionally. Only content explicitly marked as an equation is allowed
to reach the compiler as LaTeX, and even then it is checked for the handful of
constructs that would break the document rather than merely fail to render.

**Never mangle the author's mathematics.** An equation from the source is passed
through, because rewriting it would change what the author claimed. If it cannot
compile, the repair loop degrades the whole block to an image or to plain text --
a visible, reportable downgrade rather than a silent corruption.
"""

from __future__ import annotations

import re

#: Characters TeX treats specially in ordinary text, with their replacements.
#:
#: Order matters and sequential replacement is not enough. The naive approach --
#: backslash first, then braces -- corrupts its own output, because the
#: replacement for ``\`` *contains* braces which the later rule then escapes:
#: ``a\b`` became ``a\textbackslash\{\}b``. The substitution is therefore done
#: in a single pass (see :func:`escape_text`) so no replacement is ever rescanned.
_TEXT_ESCAPES: dict[str, str] = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

#: Single-pass pattern over every special character, so replacements are emitted
#: verbatim and never re-examined.
_ESCAPE_RE = re.compile(
    "|".join(re.escape(ch) for ch in _TEXT_ESCAPES)
)

#: Straight quotes produce the wrong glyphs in LaTeX; directional quotes do not.
#: Applied after escaping so the replacements themselves are not escaped.
_TYPOGRAPHY: tuple[tuple[str, str], ...] = (
    ('"', "''"),
)


def escape_text(text: str) -> str:
    """Escape manuscript prose for use in LaTeX text mode.

    CJK characters pass through untouched: ``xeCJK`` handles them, and escaping
    them would be both wrong and destructive.
    """
    if not text:
        return ""
    out = _ESCAPE_RE.sub(lambda m: _TEXT_ESCAPES[m.group(0)], text)
    for char, replacement in _TYPOGRAPHY:
        out = out.replace(char, replacement)
    # A run of spaces is collapsed by TeX anyway; normalising here keeps the
    # generated source readable, which matters because users will edit it.
    return re.sub(r"[ \t]+", " ", out).strip()


#: Constructs that must never reach the compiler from source material. These end
#: a document or redefine the environment rather than merely failing to render,
#: so an equation containing one is rejected instead of passed through.
_FORBIDDEN = (
    r"\end{document}",
    r"\begin{document}",
    r"\documentclass",
    r"\input",
    r"\include",
    r"\write18",
    r"\csname",
    r"\catcode",
    r"\def",
    r"\let",
    r"\usepackage",
)


def equation_is_safe(latex: str) -> tuple[bool, str]:
    """Whether an author's equation can be passed through to the compiler.

    Returns ``(ok, reason)``. This is a safety check, not a syntax check: a
    malformed equation is the repair loop's problem, but an equation that closes
    the document is nobody's problem to fix because the failure is not local.
    """
    lowered = latex.lower()
    for token in _FORBIDDEN:
        if token.lower() in lowered:
            return False, f"contains {token}, which would break the document"

    if latex.count("$") % 2:
        return False, "unbalanced $"
    if latex.count("{") != latex.count("}"):
        return False, "unbalanced braces"

    # Environments must close. An unclosed \begin swallows the rest of the file,
    # which turns one bad equation into a document that cannot compile at all.
    begins = re.findall(r"\\begin\{(\w+\*?)\}", latex)
    ends = re.findall(r"\\end\{(\w+\*?)\}", latex)
    if sorted(begins) != sorted(ends):
        return False, "unbalanced \\begin/\\end"

    return True, ""


def as_math(latex: str) -> str:
    """Normalise an author's equation into inline or display math.

    The source may or may not already carry delimiters. Detecting rather than
    assuming avoids the two classic outcomes: ``$$x$$`` inside ``\\[...\\]``, and
    display math dropped into text mode.
    """
    text = latex.strip()
    if not text:
        return ""

    delimited = (
        (text.startswith("$") and text.endswith("$"))
        or (text.startswith(r"\[") and text.endswith(r"\]"))
        or (text.startswith(r"\(") and text.endswith(r"\)"))
        or re.match(r"^\\begin\{(equation|align|gather|multline)\*?\}", text)
    )
    if delimited:
        return text
    return f"\\[{text}\\]"


def strip_math(latex: str) -> str:
    """Plain-text approximation of an equation, for degradation.

    Used when an equation cannot be compiled and the alternative is losing it
    entirely. Deliberately crude: the goal is that a human reading the slide can
    still tell what was there, not that the result be typographically correct.
    """
    text = latex.strip()
    text = re.sub(r"^\\\[|\\\]$", "", text)
    text = re.sub(r"^\$+|\$+$", "", text)
    text = re.sub(r"\\begin\{\w+\*?\}|\\end\{\w+\*?\}", "", text)
    text = re.sub(r"\\(frac|dfrac)\{([^{}]*)\}\{([^{}]*)\}", r"(\2)/(\3)", text)
    text = re.sub(r"\\sqrt\{([^{}]*)\}", r"sqrt(\1)", text)
    text = re.sub(r"[_^]\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\(le|leq)\b", "<=", text)
    text = re.sub(r"\\(ge|geq)\b", ">=", text)
    text = re.sub(r"\\(times)\b", "x", text)
    text = re.sub(r"\\(cdot)\b", "*", text)
    text = re.sub(r"\\[a-zA-Z]+", "", text)
    text = text.replace("{", "").replace("}", "")
    return escape_text(re.sub(r"\s+", " ", text).strip())

# --------------------------------------------------------------------------- #
# Unicode mathematics
# --------------------------------------------------------------------------- #

#: Mathematical symbols an author types directly into a word processor, mapped to
#: the LaTeX that reproduces them. A manuscript equation is frequently prose with
#: Unicode operators -- "其中泛化界的形式为 R(h) ≤ R̂(h) + O(√(d/n)) + ε" -- rather
#: than LaTeX source. Wrapping that in \[...\] compiles without complaint and
#: renders as nonsense: the Chinese vanishes and the operators drop out.
_UNICODE_MATH: tuple[tuple[str, str], ...] = (
    ("≤", r"$\leq$"), ("≥", r"$\geq$"), ("≠", r"$\neq$"), ("≈", r"$\approx$"),
    ("≡", r"$\equiv$"), ("∝", r"$\propto$"), ("∞", r"$\infty$"),
    ("×", r"$\times$"), ("÷", r"$\div$"), ("±", r"$\pm$"), ("∓", r"$\mp$"),
    ("·", r"$\cdot$"), ("−", "-"),
    ("∑", r"$\sum$"), ("∏", r"$\prod$"), ("∫", r"$\int$"),
    ("∂", r"$\partial$"), ("∇", r"$\nabla$"), ("√", r"$\surd$"),
    ("∈", r"$\in$"), ("∉", r"$\notin$"), ("⊂", r"$\subset$"),
    ("⊆", r"$\subseteq$"), ("∪", r"$\cup$"), ("∩", r"$\cap$"),
    ("∀", r"$\forall$"), ("∃", r"$\exists$"), ("∅", r"$\emptyset$"),
    ("→", r"$\to$"), ("←", r"$\leftarrow$"), ("⇒", r"$\Rightarrow$"),
    ("⇔", r"$\Leftrightarrow$"), ("↦", r"$\mapsto$"),
    ("α", r"$\alpha$"), ("β", r"$\beta$"), ("γ", r"$\gamma$"),
    ("δ", r"$\delta$"), ("ε", r"$\varepsilon$"), ("ζ", r"$\zeta$"),
    ("η", r"$\eta$"), ("θ", r"$\theta$"), ("λ", r"$\lambda$"),
    ("μ", r"$\mu$"), ("ν", r"$\nu$"), ("π", r"$\pi$"), ("ρ", r"$\rho$"),
    ("σ", r"$\sigma$"), ("τ", r"$\tau$"), ("φ", r"$\phi$"), ("χ", r"$\chi$"),
    ("ψ", r"$\psi$"), ("ω", r"$\omega$"),
    ("Γ", r"$\Gamma$"), ("Δ", r"$\Delta$"), ("Θ", r"$\Theta$"),
    ("Λ", r"$\Lambda$"), ("Σ", r"$\Sigma$"), ("Φ", r"$\Phi$"),
    ("Ω", r"$\Omega$"),
    ("̂", ""),  # combining circumflex: dropped, since \hat needs its base
)

#: Cues that a string really is LaTeX source rather than prose with symbols.
_LATEX_CUES = re.compile(
    r"\\(frac|sqrt|sum|int|prod|alpha|beta|gamma|delta|epsilon|theta|lambda|"
    r"sigma|omega|mathbb|mathcal|mathbf|hat|bar|tilde|vec|left|right|begin|"
    r"leq|geq|neq|approx|cdot|times|in|subset|to|Rightarrow|text|operatorname)"
    r"\b|[_^]\{|\$"
)


def looks_like_latex(text: str) -> bool:
    """Whether a string should be treated as LaTeX source.

    Conservative by design: prose misread as LaTeX renders as garbage, while LaTeX
    misread as prose merely renders as its own source, which a user can see and
    correct.
    """
    return bool(_LATEX_CUES.search(text))


def unicode_math_to_text(text: str) -> str:
    """Escape prose while converting Unicode operators to inline math.

    The result is text mode with ``$...$`` islands, so Chinese around the symbols
    keeps rendering through xeCJK -- which it does not inside a display equation.
    """
    if not text:
        return ""
    # Escape first, then substitute: the replacements contain backslashes and
    # dollar signs that escaping would destroy.
    out = escape_text(text)
    for symbol, replacement in _UNICODE_MATH:
        if symbol in out:
            out = out.replace(symbol, replacement)
    # Adjacent math islands are merged: "$\leq$ $\varepsilon$" sets more
    # cleanly as a single group and avoids stray inter-word space.
    return re.sub(r"\$\s*\$", " ", out)
