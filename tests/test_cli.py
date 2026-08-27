"""Tests for the command-line interface, exercised the way a user runs it.

Every other test file imports functions directly. That leaves a whole class of
defect uncovered, and this project shipped three of them:

- ``rostrum --help`` crashed on a clean install, because four dependencies were
  imported but never declared. 414 tests passed at the time.
- ``rostrum render deck.json`` -- the form the README documents -- failed, because
  the template argument was mandatory here while optional in ``build`` and
  ``preview``.
- ``rostrum build`` wrote its intermediate ``.deck.json`` beside the manuscript,
  leaving an artefact in a directory the user never nominated as an output.

None of those are visible from inside the process. They are properties of the
command surface: which arguments are accepted, which files appear on disk, what
the exit code is. So these tests drive ``main()`` through ``argv`` and assert on
the filesystem, which is what a user actually experiences.
"""

from __future__ import annotations

import contextlib
import io
import pathlib

import pytest

from rostrum.cli import _explain_missing, main

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures"
MANUSCRIPT = FIXTURES / "proposal.docx"


def run(*argv: str) -> tuple[int, str, str]:
    """Invoke the CLI as the shell would, capturing both streams."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:  # argparse exits rather than returning
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


class TestHelpAndDiscovery:
    """The commands a user runs before they have anything to render."""

    def test_help_works(self):
        code, out, _ = run("--help")
        assert code == 0
        assert "duration" in out.lower() or "presentation" in out.lower()

    def test_every_subcommand_has_help(self):
        code, out, _ = run("--help")
        for command in (
            "ingest", "render", "beamer", "preview", "point", "edit", "build",
            "themes", "inspect",
        ):
            assert command in out, f"{command} missing from help"

    def test_themes_lists_the_names_the_readme_promises(self):
        code, out, _ = run("themes")
        assert code == 0
        for theme in (
            "academic-blue", "conference-dark", "minimal-warm", "thesis-grey"
        ):
            assert theme in out, f"{theme} is documented but not listed"

    def test_beamer_themes_listable_without_a_deck(self):
        # The command a user runs to choose a preset, before they have a deck.
        code, out, _ = run("beamer", "--list-themes")
        assert code == 0
        assert "clean" in out


class TestDocumentedWorkflow:
    """The exact command sequence in the README must work."""

    def test_ingest_then_render_without_a_template(self, tmp_path):
        """``rostrum render deck.json`` with no template.

        This failed for real: the argument was mandatory in ``render`` and
        optional in ``build`` and ``preview``, so the three commands disagreed and
        the documented form errored out.
        """
        deck = tmp_path / "t.json"
        code, _, err = run(
            "ingest", str(MANUSCRIPT), "--minutes", "8", "--out", str(deck)
        )
        assert code == 0, err
        assert deck.exists()

        pptx = tmp_path / "t.pptx"
        code, out, err = run("render", str(deck), "--out", str(pptx))
        assert code == 0, err
        assert pptx.exists()

    def test_render_accepts_a_named_theme(self, tmp_path):
        deck = tmp_path / "t.json"
        run("ingest", str(MANUSCRIPT), "--minutes", "8", "--out", str(deck))
        pptx = tmp_path / "t.pptx"
        code, out, err = run(
            "render", str(deck), "--theme", "thesis-grey", "--out", str(pptx)
        )
        assert code == 0, err
        assert "thesis-grey" in out

    def test_build_produces_slides_and_a_script(self, tmp_path):
        pptx = tmp_path / "talk.pptx"
        code, out, err = run(
            "build", str(MANUSCRIPT), "--minutes", "8", "--out", str(pptx)
        )
        assert code == 0, err
        assert pptx.exists()
        # The script is the other half of the dual-channel promise.
        assert (tmp_path / "talk.script.md").exists()

    def test_build_reports_whether_the_talk_fits(self, tmp_path):
        code, out, _ = run(
            "build", str(MANUSCRIPT), "--minutes", "8",
            "--out", str(tmp_path / "t.pptx"),
        )
        assert code == 0
        assert "duration fit" in out

    def test_build_does_not_write_beside_the_manuscript(self, tmp_path):
        """The intermediate IR must follow --out, not litter the input folder."""
        manuscript = tmp_path / "input" / "paper.docx"
        manuscript.parent.mkdir()
        manuscript.write_bytes(MANUSCRIPT.read_bytes())
        outdir = tmp_path / "output"

        code, _, err = run(
            "build", str(manuscript), "--minutes", "8",
            "--out", str(outdir / "talk.pptx"),
        )
        assert code == 0, err
        strays = list(manuscript.parent.glob("*.deck.json"))
        assert strays == [], f"wrote into the manuscript's directory: {strays}"
        assert (outdir / "talk.deck.json").exists()

    def test_unknown_theme_fails_before_doing_work(self, tmp_path):
        """A bad theme name must not leave half-built artefacts behind."""
        outdir = tmp_path / "o"
        code, out, err = run(
            "build", str(MANUSCRIPT), "--theme", "no-such-theme",
            "--minutes", "8", "--out", str(outdir / "t.pptx"),
        )
        assert code != 0
        assert "no-such-theme" in (out + err)
        # The available names must be offered, not just the rejection.
        assert "academic-blue" in (out + err)


class TestBadInput:
    """Errors a user will actually hit, and whether they can act on them."""

    def test_missing_file_is_reported_by_name(self, tmp_path):
        code, out, err = run(
            "ingest", str(tmp_path / "nope.docx"), "--out", str(tmp_path / "o.json")
        )
        assert code != 0
        assert "nope.docx" in (out + err)

    def test_unsupported_format_lists_what_is_supported(self, tmp_path):
        bad = tmp_path / "notes.rtf"
        bad.write_text("x")
        code, out, err = run(
            "ingest", str(bad), "--out", str(tmp_path / "o.json")
        )
        assert code != 0
        combined = out + err
        assert ".docx" in combined and ".tex" in combined

    def test_malformed_deck_json_is_not_a_traceback(self, tmp_path):
        broken = tmp_path / "b.json"
        broken.write_text("{not json")
        code, out, err = run("render", str(broken), "--out", str(tmp_path / "o.pptx"))
        assert code != 0
        assert "Traceback" not in (out + err)


class TestMissingDependencyMessages:
    """A missing optional dependency must say what to install.

    ``ModuleNotFoundError: No module named 'fitz'`` names a module that bears no
    resemblance to the package providing it (PyMuPDF), which is useless to
    someone who just followed the README.
    """

    @pytest.mark.parametrize(
        "module,expected",
        [
            ("fitz", "rostrum[pdf]"),
            ("docx", "python-docx"),
            ("PIL", "Pillow"),
            ("lxml", "lxml"),
        ],
    )
    def test_hint_names_the_package_not_the_module(self, module, expected):
        message = _explain_missing(ModuleNotFoundError(name=module))
        assert "pip install" in message
        assert expected in message

    def test_unknown_module_still_produces_something_useful(self):
        message = _explain_missing(ModuleNotFoundError(name="whatever"))
        assert "whatever" in message
