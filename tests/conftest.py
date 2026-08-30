"""Shared pytest configuration.

Its main job is a guard rail: fail loudly when a *required* dependency is missing,
instead of skipping the tests that need it.

The distinction matters because skipping is silent. ``python-pptx`` and
``python-docx`` are declared dependencies now, so their absence means the install
is broken -- and a broken install reported as "42 skipped" reads as a pass. The
project has already been bitten by a variant of this: an ``importorskip`` call
evaluated inside a ``skipif`` argument aborted collection for a whole module,
costing 57 tests, and pytest summarised the loss as "1 skipped".

Genuinely optional extras (PyMuPDF, a LaTeX engine, poppler) still skip, because
their absence is a legitimate configuration rather than a fault. Those skips are
listed at the end of a run so the coverage gap is visible rather than buried.
"""

from __future__ import annotations

import importlib.util
import shutil

import pytest

#: Packages the project declares as required. Missing one is an install problem,
#: not a reason to skip.
_REQUIRED = {
    "pptx": "python-pptx",
    "docx": "python-docx",
    "lxml": "lxml",
    "PIL": "Pillow",
    "fontTools": "fonttools",
    "pydantic": "pydantic",
}

#: Optional capabilities. Absence is reported, never fatal.
_OPTIONAL_MODULES = {"fitz": "PyMuPDF — PDF manuscripts"}
_OPTIONAL_BINARIES = {
    "xelatex": "TeX Live — Beamer output",
    "pdftotext": "poppler-utils — Beamer overflow checking",
    "soffice": "LibreOffice — PPTX render verification",
}


def pytest_sessionstart(session: pytest.Session) -> None:
    """Refuse to run at all if a required dependency is absent.

    Better a clear failure at startup than a green summary over silently skipped
    work. The message names the package to install, because the module name is not
    always a usable hint -- nobody guesses "PyMuPDF" from "fitz".
    """
    missing = [
        package
        for module, package in _REQUIRED.items()
        if importlib.util.find_spec(module) is None
    ]
    if missing:
        raise pytest.UsageError(
            "required dependencies are missing: "
            + ", ".join(missing)
            + "\nInstall the project first:  pip install -e \".[dev]\""
        )


def _absent_capabilities() -> list[str]:
    """Optional capabilities this environment lacks, described for a human."""
    absent = [
        f"{name} ({why})"
        for name, why in _OPTIONAL_MODULES.items()
        if importlib.util.find_spec(name) is None
    ]
    absent += [
        f"{name} ({why})"
        for name, why in _OPTIONAL_BINARIES.items()
        if shutil.which(name) is None
    ]
    return absent


def pytest_report_header(config: pytest.Config) -> list[str]:
    """Announce absent optional capabilities before the run."""
    absent = _absent_capabilities()
    if not absent:
        return ["all optional capabilities present: full coverage"]
    return [
        "optional capabilities absent: " + "; ".join(absent),
        "  -> some tests will skip. Install these for full coverage.",
    ]


def pytest_terminal_summary(terminalreporter) -> None:  # type: ignore[no-untyped-def]
    """Repeat the warning after the run, where it cannot be missed.

    The header alone is not enough: this project runs pytest under ``-q``, which
    suppresses headers entirely. A coverage gap that only announces itself in a
    mode nobody uses is not announced at all.
    """
    absent = _absent_capabilities()
    if not absent:
        return
    terminalreporter.write_line("")
    terminalreporter.write_line(
        "note: reduced coverage — " + "; ".join(absent), yellow=True
    )
    terminalreporter.write_line(
        '      install with:  pip install -e ".[dev,pdf]"  (plus system packages)'
    )
