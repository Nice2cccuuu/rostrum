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
    pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
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
    # Create the parent directory rather than failing on it: being told
    # "No such file or directory" for an output path you just specified is a
    # pointless obstacle.
    deck_path.parent.mkdir(parents=True, exist_ok=True)
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


def cmd_edit(args: argparse.Namespace) -> int:
    """Revise a deck by describing the change in words.

    Interactive by default because revision is a conversation: the user says
    something, sees exactly what it would do, and either accepts it or rephrases.
    Batch mode (``--say``) exists for scripting and for the test suite.

    Confident edits apply immediately; anything below the threshold is shown as a
    diff and waits for confirmation. That asymmetry is the point -- a tool that
    asks about everything is tiresome, and one that asks about nothing cannot be
    trusted with a deck you have to defend.
    """
    from rostrum.ir.nodes import Deck
    from rostrum.patch.ops import EditLog
    from rostrum.patch.session import Session

    deck_path = pathlib.Path(args.deck)
    deck = Deck.model_validate_json(deck_path.read_text(encoding="utf-8"))

    capacity = None
    if args.template:
        from rostrum.templates import bind, capacity_caps, ingest_pptx

        contract, ingest_report = ingest_pptx(
            args.template, template_id=pathlib.Path(args.template).stem
        )
        for w in ingest_report.warnings:
            print(f"template: {w}", file=sys.stderr)
        capacity = capacity_caps(bind(deck, contract))
        print(f"measured {len(contract.layouts)} layouts from {args.template}")

    session = Session(original=deck, capacity=capacity)

    log_path = pathlib.Path(args.log) if args.log else deck_path.with_suffix(".log.json")
    if args.replay:
        stored = EditLog.model_validate_json(
            pathlib.Path(args.replay).read_text(encoding="utf-8")
        )
        session.replay(stored)
        print(f"replayed {len(stored.patches)} patch(es) from {args.replay}")

    selection = args.select or []
    if args.say:
        for utterance in args.say:
            _run_utterance(session, utterance, selection, args)
    else:
        _repl(session, selection, args)

    _save_session(session, deck_path, log_path, args)
    return 0


def _run_utterance(session, utterance: str, selection: list[str], args) -> None:
    result, diff, report = session.say(
        utterance, selection=selection, threshold=args.threshold
    )
    print(f"> {utterance}")
    if not result.ok:
        print(f"  未执行：{result.reason}")
        if result.question:
            print(f"  {result.question}")
        return

    print(f"  读作：{result.reason}  [置信 {result.confidence:.2f}]")
    for line in result.evidence:
        print(f"    · {line}")
    if diff:
        for line in diff.render().splitlines():
            print(f"  {line}")

    if report is None:
        print("  未应用：置信不足，需确认（加 --yes 可直接应用）")
        if args.yes:
            session.apply(result.patch)
            print("  已按 --yes 应用")
    else:
        print(f"  已应用：{report.summary()}")
    for note in (report.notes if report else []):
        print(f"  note: {note}")


def _repl(session, selection: list[str], args) -> None:
    print("说出你的修改要求，一行一条。")
    print("  :undo  撤销    :redo  重做    :log  编辑历史")
    print("  :diff  累计改动 :pages 页面清单 :quit 保存并退出")
    print()
    while True:
        try:
            line = input("rostrum> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in (":quit", ":q", ":exit"):
            return
        if line == ":undo":
            undone = session.undo()
            print(f"  已撤销：{undone.utterance}" if undone else "  没有可撤销的操作")
            continue
        if line == ":redo":
            redone = session.redo()
            print(f"  已重做：{redone.utterance}" if redone else "  没有可重做的操作")
            continue
        if line == ":log":
            history = session.history()
            print("\n".join(f"  {h}" for h in history) if history else "  （无改动）")
            continue
        if line == ":diff":
            print(session.total_diff().render())
            continue
        if line == ":pages":
            for i, (_, slide) in enumerate(session.current.iter_slides(), 1):
                dwell = f"{slide.dwell_seconds:.0f}s" if slide.dwell_seconds else "-"
                lock = " 🔒" if slide.dwell_locked else ""
                print(f"  {i:2d}. {slide.title or '(无标题)'}  "
                      f"[{len(slide.blocks)}块 {dwell}{lock}]")
            continue
        _run_utterance(session, line, selection, args)


def _save_session(session, deck_path: pathlib.Path, log_path: pathlib.Path, args) -> None:
    if not session.log.patches:
        print("no changes; nothing written")
        return
    out = pathlib.Path(args.out) if args.out else deck_path
    out.write_text(session.current.model_dump_json(indent=2), encoding="utf-8")
    log_path.write_text(session.log.model_dump_json(indent=2), encoding="utf-8")
    print()
    print(f"applied {len(session.log.patches)} patch(es)")
    print(f"wrote {out}")
    print(f"wrote {log_path}  (replay with: rostrum edit {out} --replay {log_path})")
    print()
    print("重新出片： rostrum render " + str(out) + " [模板] --out slides.pptx")


def cmd_preview(args: argparse.Namespace) -> int:
    """Render page images plus the anchor map that makes them clickable.

    The anchor file is the contract between a preview and the revision engine: it
    maps a normalised point to an IR uid, so a UI needs no knowledge of the deck
    structure to let a user point at a bullet. Coordinates are normalised, so the
    same file works whatever resolution the preview is rendered at.
    """
    from rostrum.ir.nodes import Deck
    from rostrum.render import render_pptx
    from rostrum.render.anchors import draw_overlay
    from rostrum.templates import bind, capacity_caps, ingest_pptx

    deck = Deck.model_validate_json(
        pathlib.Path(args.deck).read_text(encoding="utf-8")
    )
    template_path, scratch = _resolve_template(args)
    try:
        contract, ingest_report = ingest_pptx(
            template_path,
            template_id=pathlib.Path(template_path).stem,
            font_path=args.font,
        )
        for w in ingest_report.warnings:
            print(f"template: {w}", file=sys.stderr)

        binding = bind(deck, contract)
        from rostrum.budget.allocate import allocate

        allocate(deck, apply=True, capacity=capacity_caps(binding))

        outdir = pathlib.Path(args.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        pptx_path = outdir / "preview.pptx"
        report = render_pptx(
            deck, contract, binding, str(pptx_path), font_path=args.font
        )
    finally:
        if scratch:
            with contextlib.suppress(OSError):
                os.unlink(scratch)

    anchors_path = outdir / "anchors.json"
    report.anchors.save(str(anchors_path))
    print(f"wrote {pptx_path}  ({report.slides_written} slides)")
    print(f"wrote {anchors_path}  ({len(report.anchors.anchors)} anchors)")

    images = _rasterise(pptx_path, outdir, dpi=args.dpi)
    if not images:
        print(
            "note: no page images written (LibreOffice not found). The anchor "
            "map is still valid -- a UI can rasterise however it likes.",
            file=sys.stderr,
        )
        return 0

    print(f"wrote {len(images)} page image(s) to {outdir}")
    if args.overlay:
        for index, image in enumerate(images):
            out = outdir / f"overlay-{index + 1:02d}.png"
            draw_overlay(report.anchors, index, str(image), str(out))
        print(f"wrote {len(images)} overlay(s) — anchor boxes drawn on each page")
    return 0


def _rasterise(pptx_path: pathlib.Path, outdir: pathlib.Path, *, dpi: int) -> list:
    """Render pages to PNG via LibreOffice, if it is available.

    Optional on purpose: the anchor map is the real product here, and a UI will
    usually rasterise on its own. Missing LibreOffice must not fail the command.
    """
    import glob
    import shutil
    import subprocess

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return []

    try:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir",
             str(outdir), str(pptx_path)],
            check=True, capture_output=True, timeout=180,
        )
        pdf = outdir / (pptx_path.stem + ".pdf")
        if not pdf.exists():
            return []
        subprocess.run(
            ["pdftoppm", "-png", "-r", str(dpi), str(pdf), str(outdir / "page")],
            check=True, capture_output=True, timeout=180,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    return sorted(glob.glob(str(outdir / "page-*.png")))


def cmd_point(args: argparse.Namespace) -> int:
    """Resolve a click or a lasso to IR uids, then optionally edit them.

    This is the click-to-select half of revision. The same uids feed the same
    ``Patch`` mechanism that a purely textual request uses -- pointing replaces
    the *description* of a target, never the operation itself.
    """
    from rostrum.render.anchors import AnchorMap, Box, hit_test, lasso

    amap = AnchorMap.load(args.anchors)

    if args.rect:
        x, y, w, h = args.rect
        result = lasso(amap, args.slide, Box(x=x, y=y, w=w, h=h))
        gesture = f"矩形 ({x:.2f},{y:.2f}) {w:.2f}×{h:.2f}"
    else:
        if args.at is None:
            print("point needs --at X Y or --rect X Y W H", file=sys.stderr)
            return 2
        x, y = args.at
        result = hit_test(amap, args.slide, x, y, tolerance=args.tolerance)
        gesture = f"点击 ({x:.3f}, {y:.3f})"

    print(f"第 {args.slide + 1} 页 {gesture}")
    if not result.hits:
        print("  该位置没有可编辑的内容")
        return 1
    print(result.describe())

    if result.ambiguous:
        print("  ⚠ 前两个候选得分接近，建议让用户确认再改")

    if not args.say:
        return 0

    from rostrum.ir.nodes import Deck
    from rostrum.patch.session import Session

    if not args.deck:
        print("--say needs --deck to edit", file=sys.stderr)
        return 2

    deck_path = pathlib.Path(args.deck)
    deck = Deck.model_validate_json(deck_path.read_text(encoding="utf-8"))
    session = Session(original=deck)

    selection = result.uids if args.rect else [result.best.uid]
    print()
    for utterance in args.say:
        _run_utterance(session, utterance, selection, args)

    if session.log.patches:
        out = pathlib.Path(args.out) if args.out else deck_path
        out.write_text(session.current.model_dump_json(indent=2), encoding="utf-8")
        print()
        print(f"wrote {out}")
    return 0


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

    pv = sub.add_parser(
        "preview", help="render page images plus a clickable anchor map"
    )
    pv.add_argument("deck", help="deck IR json")
    pv.add_argument("template", nargs="?", help="template pptx (default: a theme)")
    pv.add_argument("--theme", help="built-in theme id when no template is given")
    pv.add_argument("--outdir", default="preview", help="output directory")
    pv.add_argument("--dpi", type=int, default=110, help="page image resolution")
    pv.add_argument("--font", help="font file for measurement")
    pv.add_argument(
        "--overlay",
        action="store_true",
        help="draw anchor boxes onto each page, for verifying the geometry",
    )
    pv.set_defaults(func=cmd_preview)

    pt = sub.add_parser(
        "point", help="resolve a click or lasso to uids, and optionally edit them"
    )
    pt.add_argument("anchors", help="anchors.json from `rostrum preview`")
    pt.add_argument("--slide", type=int, default=0, help="zero-based page index")
    pt.add_argument(
        "--at", nargs=2, type=float, metavar=("X", "Y"),
        help="normalised click position, 0-1 on each axis",
    )
    pt.add_argument(
        "--rect", nargs=4, type=float, metavar=("X", "Y", "W", "H"),
        help="normalised selection rectangle",
    )
    pt.add_argument(
        "--tolerance", type=float, default=0.02,
        help="how far beyond a box a click still counts (default: 0.02)",
    )
    pt.add_argument(
        "--say", action="append", metavar="UTTERANCE",
        help="edit what was selected; repeatable",
    )
    pt.add_argument("--deck", help="deck IR to edit, required with --say")
    pt.add_argument("--out", help="write the revised deck here")
    pt.add_argument("--threshold", type=float, default=0.75)
    pt.add_argument("--yes", action="store_true")
    pt.set_defaults(func=cmd_point)

    e = sub.add_parser("edit", help="revise a deck by describing the change")
    e.add_argument("deck", help="deck IR json produced by ingest or build")
    e.add_argument(
        "--say",
        action="append",
        metavar="UTTERANCE",
        help="apply one instruction and exit; repeatable, applied in order",
    )
    e.add_argument(
        "--select",
        action="append",
        metavar="UID",
        help="uid the user has selected in a preview; repeatable",
    )
    e.add_argument(
        "--template",
        help="template to measure, so re-budgeting respects real capacity",
    )
    e.add_argument("--out", help="write the revised deck here (default: in place)")
    e.add_argument("--log", help="edit-log path (default: <deck>.log.json)")
    e.add_argument("--replay", metavar="LOG", help="replay a stored edit log first")
    e.add_argument(
        "--threshold",
        type=float,
        default=0.75,
        help="apply automatically at or above this confidence (default: 0.75)",
    )
    e.add_argument(
        "--yes",
        action="store_true",
        help="apply low-confidence edits without asking",
    )
    e.set_defaults(func=cmd_edit)

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
