"""Command-line interface.

Four commands, matching what a user actually does:

    rostrum inspect <template.pptx>            what can this template hold?
    rostrum ingest  <manuscript>               manuscript to deck IR
    rostrum render  <deck.json> <tpl.pptx>     build the deck and its script
    rostrum build   <manuscript> <tpl.pptx>    both of the above, in one step

Every command prints the CI metrics (overflow rate, duration fit) so a problem is
visible immediately rather than at slide 14 of a rehearsal.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import sys
import tempfile

from rostrum import __version__
from rostrum.budget import allocate, estimate_duration
from rostrum.ir import Deck, validate
from rostrum.templates import bind, capacity_caps, ingest_pptx, title_overflows
from rostrum.themes import DEFAULT_THEME_ID


def _default_font() -> str | None:
    """Pick a CJK-capable font from the usual locations.

    Measurement accuracy depends on using the font the deck will actually render
    with, so this is only a convenience default.
    """
    for candidate in (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "C:/Windows/Fonts/msyh.ttc",
    ):
        if pathlib.Path(candidate).exists():
            return candidate
    return None


def cmd_inspect(args: argparse.Namespace) -> int:
    contract, report = ingest_pptx(
        args.template,
        font_path=args.font or _default_font(),
        language=args.language,
    )
    print(f"{contract.name}  [{contract.renderer.value}]  {contract.page_aspect}")
    print(f"layouts: {report.layouts_kept} usable of {report.layouts_found}")
    if contract.fonts:
        print(f"fonts:   {', '.join(sorted(contract.fonts))}")
    print()

    for layout in contract.layouts:
        roles = ", ".join(r.value for r in layout.roles)
        print(f"  {layout.layout_id}")
        print(f"    roles: {roles}")
        for slot in layout.slots:
            cap = (
                f"{slot.capacity_units:>5} units / {slot.capacity_lines} lines"
                if slot.capacity_units is not None
                else "    visual"
            )
            print(
                f"    {slot.slot_id:<8} {slot.kind:<9} "
                f"{slot.font_size_pt:>5.1f}pt  {cap}"
            )
        print()

    print("roles covered: " + ", ".join(sorted(r.value for r in report.roles_covered)))
    for w in report.warnings:
        print(f"warning: {w}", file=sys.stderr)

    if args.out:
        pathlib.Path(args.out).write_text(
            contract.model_dump_json(indent=2), encoding="utf-8"
        )
        print(f"\ncontract written to {args.out}")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    deck = Deck.model_validate_json(
        pathlib.Path(args.deck).read_text(encoding="utf-8")
    )
    font = args.font or _default_font()

    contract, ingest_report = ingest_pptx(
        args.template, font_path=font, language=deck.meta.language
    )
    for w in ingest_report.warnings:
        print(f"template: {w}", file=sys.stderr)

    # Bind before allocating: the budget must be clamped by what the chosen
    # layout was measured to hold.
    binding = bind(deck, contract)
    for w in binding.warnings:
        print(f"binding: {w}", file=sys.stderr)
    if binding.missing_roles:
        print(
            "template cannot host: "
            + ", ".join(sorted(r.value for r in binding.missing_roles)),
            file=sys.stderr,
        )

    plan = allocate(deck, capacity=capacity_caps(binding))

    report = validate(deck, strict_provenance=not args.lenient)
    print(f"validation: {report.summary()}")
    for f in report.findings:
        stream = sys.stderr if f.severity.value == "error" else sys.stdout
        print(f"  {f}", file=stream)
    if not report.ok and not args.force:
        print(
            "\nrefusing to render: fix the errors above, or pass --force",
            file=sys.stderr,
        )
        return 1

    from rostrum.render import export_script, render_pptx

    out = args.out or str(pathlib.Path(args.deck).with_suffix(".pptx"))
    render = render_pptx(
        deck, contract, binding, out, font_path=font,
        include_backup=not args.no_backup,
    )

    script_path = args.script or str(pathlib.Path(out).with_suffix(".script.md"))
    export_script(deck, script_path)

    # -- metrics -------------------------------------------------------- #
    spoken = estimate_duration(deck)
    fit = spoken / deck.delivery.total_seconds if deck.delivery.total_seconds else 0

    print()
    print(f"wrote {out}  ({render.slides_written} slides)")
    print(f"wrote {script_path}")
    print()
    print(f"budget units    : {plan.total_units}")
    print(f"slides target   : {plan.target_slide_count} (actual "
          f"{plan.actual_slide_count})")
    print(f"overflow rate   : {render.overflow_rate:.1%}")
    print(f"duration fit    : {fit:.2f}  ({spoken:.0f}s of "
          f"{deck.delivery.total_seconds}s)")
    if render.shrunk_slots:
        print(f"autofit shrinks : {len(render.shrunk_slots)}")
    for note in plan.notes:
        print(f"note: {note}")
    for path, slot, ratio in render.overflowed_slots:
        print(f"overflow: {path}/{slot} at {ratio:.2f}x", file=sys.stderr)
    for uid, used, cap in title_overflows(deck, binding):
        print(
            f"title too long: {deck.path_of(uid)} uses {used} of {cap} units",
            file=sys.stderr,
        )
    for uid in render.demoted_to_notes:
        print(f"moved to notes: {uid}", file=sys.stderr)

    return 0 if render.ok else 2


# --------------------------------------------------------------------------- #
# ingest
# --------------------------------------------------------------------------- #

_PARSERS = {
    ".docx": "docx",
    ".doc": "docx",
    ".pdf": "pdf",
    ".tex": "latex",
    ".latex": "latex",
}


def _parse_manuscript(path: str, *, language: str, minutes: float):
    """Dispatch to the parser for this file type."""
    suffix = pathlib.Path(path).suffix.lower()
    kind = _PARSERS.get(suffix)
    if kind is None:
        raise ValueError(
            f"unsupported manuscript type {suffix!r}; "
            f"expected one of {', '.join(sorted(_PARSERS))}"
        )

    if kind == "docx":
        from rostrum.ingest.docx_parser import parse_docx

        return parse_docx(path, language=language)
    if kind == "pdf":
        from rostrum.ingest.pdf_parser import parse_pdf

        return parse_pdf(path, language=language)
    from rostrum.ingest.latex_parser import parse_latex

    return parse_latex(path, language=language)


def cmd_themes(args: argparse.Namespace) -> int:
    """List the built-in themes, or write one out as a .pptx."""
    from rostrum.themes import build_template, contrast_ratio, get_theme, list_themes

    if args.export:
        theme = get_theme(args.export)
        out = args.out or f"{theme.theme_id}.pptx"
        build_template(theme, out)
        print(f"wrote {out}")
        print(
            "edit it in PowerPoint and pass it back with --template to keep your "
            "changes"
        )
        return 0

    for theme in list_themes():
        p = theme.palette
        mark = "  (default)" if theme.theme_id == DEFAULT_THEME_ID else ""
        print(f"{theme.theme_id}{mark}")
        print(f"  {theme.name} - {theme.description}")
        print(
            f"  body {theme.type_scale.body:.0f}pt on #{p.background}, "
            f"contrast {contrast_ratio(p.body, p.background):.1f}:1"
        )
        print()
    print("export one with: rostrum themes --export <id>")
    return 0


def _resolve_template(args: argparse.Namespace) -> tuple[str, object | None]:
    """Return the template path to use, building a built-in theme if needed.

    A user who supplies no template gets a real designed one rather than
    PowerPoint's blank default. The generated file is kept in a temporary
    location and measured through the ordinary ingest path -- built-in themes get
    no privileged treatment.
    """
    if getattr(args, "template", None):
        return args.template, None

    from rostrum.themes import build_template, get_theme

    theme = get_theme(getattr(args, "theme", None) or DEFAULT_THEME_ID)
    handle, scratch = tempfile.mkstemp(suffix=".pptx")
    os.close(handle)
    build_template(theme, scratch)
    print(f"using built-in theme {theme.theme_id!r} ({theme.name})")
    return scratch, scratch


def cmd_ingest(args: argparse.Namespace) -> int:
    from rostrum.ingest.planner import plan_deck
    from rostrum.ir.enums import Density, Scenario

    doc = _parse_manuscript(
        args.manuscript, language=args.language, minutes=args.minutes
    )
    for w in doc.warnings:
        print(f"parse: {w}", file=sys.stderr)

    print(f"parsed {doc.source_format}: {len(doc.text)} chars, "
          f"{len(doc.segments)} segments, {len(doc.assets)} assets")

    kinds: dict[str, int] = {}
    for seg in doc.segments:
        kinds[seg.kind.value] = kinds.get(seg.kind.value, 0) + 1
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(kinds.items())))

    deck = plan_deck(
        doc,
        total_seconds=int(args.minutes * 60),
        density=Density(args.density),
        scenario=Scenario(args.scenario),
        presenter=args.presenter,
    )
    print()
    print(f"planned: {len(deck.sections)} sections, "
          f"{sum(len(s.slides) for s in deck.sections)} slides")
    for section in deck.sections:
        rubric = section.rubric_key or "-"
        print(f"  [{rubric:<12}] w={section.weight:<4} {section.title}")
        for slide in section.slides:
            flag = " (backup)" if slide.is_backup else ""
            shown = len(slide.slide_blocks())
            spoken = len(slide.script_blocks())
            print(f"      {slide.role.value:<13} {slide.title[:34]:<34} "
                  f"{shown} on screen, {spoken} spoken{flag}")

    report = validate(deck, strict_provenance=False)
    print()
    print(f"validation: {report.summary()}")
    for f in report.findings:
        if f.severity.value != "info":
            print(f"  {f}")

    out = args.out or str(pathlib.Path(args.manuscript).with_suffix(".deck.json"))
    pathlib.Path(out).write_text(deck.model_dump_json(indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    print("next: rostrum render " + out + " <template.pptx>")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    """Manuscript straight to slides -- the path most users want.

    Kept as a thin composition of ingest and render rather than a separate code
    path, so the two-step flow and the one-step flow cannot diverge.
    """
    from rostrum.ingest.planner import plan_deck
    from rostrum.ir.enums import Density, Scenario

    doc = _parse_manuscript(
        args.manuscript, language=args.language, minutes=args.minutes
    )
    for w in doc.warnings:
        print(f"parse: {w}", file=sys.stderr)

    deck = plan_deck(
        doc,
        total_seconds=int(args.minutes * 60),
        density=Density(args.density),
        scenario=Scenario(args.scenario),
        presenter=args.presenter,
    )
    deck_path = pathlib.Path(
        args.deck_out or str(pathlib.Path(args.manuscript).with_suffix(".deck.json"))
    )
    deck_path.write_text(deck.model_dump_json(indent=2), encoding="utf-8")
    print(f"parsed {doc.source_format}: {len(doc.segments)} segments, "
          f"{len(doc.assets)} assets")
    print(f"planned {sum(len(s.slides) for s in deck.sections)} slides "
          f"across {len(deck.sections)} sections")
    print(f"wrote {deck_path}")
    print()

    template_path, scratch = _resolve_template(args)
    render_args = argparse.Namespace(
        deck=str(deck_path),
        template=template_path,
        out=args.out,
        script=args.script,
        font=args.font,
        lenient=True,  # planner output is sourced but not yet confirmed
        force=args.force,
        no_backup=args.no_backup,
    )
    try:
        return cmd_render(render_args)
    finally:
        if scratch:
            with contextlib.suppress(OSError):
                os.unlink(scratch)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rostrum",
        description="Duration-aware presentation generation for academic talks",
    )
    p.add_argument("--version", action="version", version=f"rostrum {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("inspect", help="report what a template can hold")
    i.add_argument("template")
    i.add_argument("--font", help="font used for measurement")
    i.add_argument("--language", default="zh", choices=["zh", "en", "mixed"])
    i.add_argument("--out", help="write the measured contract as JSON")
    i.set_defaults(func=cmd_inspect)

    r = sub.add_parser("render", help="render a deck IR against a template")
    r.add_argument("deck", help="deck IR JSON")
    r.add_argument("template", help=".pptx template")
    r.add_argument("--out", help="output .pptx path")
    r.add_argument("--script", help="output speaker script path")
    r.add_argument("--font", help="font used for measurement")
    r.add_argument(
        "--lenient",
        action="store_true",
        help="downgrade unsourced claims to warnings",
    )
    r.add_argument(
        "--force", action="store_true", help="render despite validation errors"
    )
    r.add_argument("--no-backup", action="store_true", help="omit reserve slides")
    r.set_defaults(func=cmd_render)

    g = sub.add_parser("ingest", help="parse a manuscript into a deck IR")
    g.add_argument("manuscript", help=".docx, .pdf or .tex")
    g.add_argument(
        "--minutes",
        type=float,
        default=8.0,
        help="talk length; drives the whole content budget (default: 8)",
    )
    g.add_argument(
        "--density",
        default="balanced",
        choices=["sparse", "balanced", "compact"],
        help="how much text per slide (default: balanced)",
    )
    g.add_argument(
        "--scenario",
        default="grant_defense",
        choices=[
            "academic_talk", "conference_oral", "grant_defense",
            "thesis_defense", "group_meeting", "generic",
        ],
        help="drives section ordering and the rubric check",
    )
    g.add_argument("--presenter", help="name on the cover")
    g.add_argument("--language", default="zh", choices=["zh", "en", "mixed"])
    g.add_argument("--out", help="output deck IR path")
    g.set_defaults(func=cmd_ingest)

    t = sub.add_parser("themes", help="list or export the built-in themes")
    t.add_argument("--export", metavar="ID", help="write a theme out as .pptx")
    t.add_argument("--out", help="output path for --export")
    t.set_defaults(func=cmd_themes)

    b = sub.add_parser("build", help="manuscript straight to slides and script")
    b.add_argument("manuscript", help=".docx, .pdf or .tex")
    b.add_argument(
        "template",
        nargs="?",
        help=".pptx template; omit to use a built-in theme",
    )
    b.add_argument(
        "--theme",
        default=None,
        help=f"built-in theme when no template is given (default: {DEFAULT_THEME_ID})",
    )
    b.add_argument("--minutes", type=float, default=8.0)
    b.add_argument(
        "--density", default="balanced",
        choices=["sparse", "balanced", "compact"],
    )
    b.add_argument(
        "--scenario", default="grant_defense",
        choices=[
            "academic_talk", "conference_oral", "grant_defense",
            "thesis_defense", "group_meeting", "generic",
        ],
    )
    b.add_argument("--presenter")
    b.add_argument("--language", default="zh", choices=["zh", "en", "mixed"])
    b.add_argument("--out", help="output .pptx path")
    b.add_argument("--script", help="output speaker script path")
    b.add_argument("--deck-out", help="also keep the intermediate deck IR here")
    b.add_argument("--font")
    b.add_argument("--force", action="store_true")
    b.add_argument("--no-backup", action="store_true")
    b.set_defaults(func=cmd_build)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
