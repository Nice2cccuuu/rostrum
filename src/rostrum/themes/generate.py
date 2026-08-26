"""Build a real ``.pptx`` template from a :class:`Theme`.

Why this is written against raw OOXML rather than python-pptx alone: python-pptx
can add slides to an existing presentation, but it cannot create a slide master,
define layouts, or write a theme part. Those are exactly the things that make a
template a template. So the generator builds the package parts directly and
assembles them into a zip.

The output is an ordinary ``.pptx`` with no Rostrum-specific markings. It goes
through the same ``ingest_pptx`` measurement as a user-supplied file, which is
the point: a built-in theme gets no privileged path, so if measurement is wrong
for built-ins it is wrong for everyone and will be caught here first.

Each layout carries its decoration (title rule, accent bar, section band, page
number) in the *layout* rather than on individual slides, so a rendered deck
inherits it automatically and a user editing the deck in PowerPoint can still
change it globally from the master.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import zipfile

from rostrum.themes.spec import Theme

EMU_PER_INCH = 914400
EMU_PER_PT = 12700


# --------------------------------------------------------------------------- #
# Layout definitions
# --------------------------------------------------------------------------- #

# Each entry: (layout_name, builder_key). The name is what the ingest layer's
# role classifier reads, so these strings are load-bearing -- "Picture" must
# appear in the big-figure layout's name for it to be recognised.
_LAYOUTS: tuple[tuple[str, str], ...] = (
    ("Title Slide", "cover"),
    ("Agenda", "agenda"),
    ("Section Header", "section"),
    ("Title and Content", "content"),
    ("Content with Picture", "text_figure"),
    ("Picture Only", "big_figure"),
    ("Two Content", "two_column"),
    ("Table", "table"),
    ("Equation", "equation"),
    ("Summary", "summary"),
    ("Thank You", "thanks"),
    ("Backup", "backup"),
)


def build_template(theme: Theme, out_path: str) -> str:
    """Write a ``.pptx`` template for ``theme`` and return its path."""
    width, height = theme.slide_size_emu()
    work = tempfile.mkdtemp(prefix="rostrum-theme-")
    try:
        _write_parts(theme, work, width, height)
        _zip_package(work, out_path)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out_path


def _write_parts(theme: Theme, root: str, width: int, height: int) -> None:
    layouts = [(i + 1, name, key) for i, (name, key) in enumerate(_LAYOUTS)]

    _put(root, "[Content_Types].xml", _content_types(len(layouts)))
    _put(root, "_rels/.rels", _root_rels())
    _put(root, "docProps/core.xml", _core_props(theme))
    _put(root, "docProps/app.xml", _app_props(theme))
    _put(root, "ppt/presentation.xml", _presentation(layouts, width, height))
    _put(root, "ppt/_rels/presentation.xml.rels", _presentation_rels())
    _put(root, "ppt/presProps.xml", _pres_props())
    _put(root, "ppt/viewProps.xml", _view_props())
    _put(root, "ppt/tableStyles.xml", _table_styles(theme))
    _put(root, "ppt/theme/theme1.xml", _theme_part(theme))
    _put(root, "ppt/slideMasters/slideMaster1.xml", _slide_master(theme, layouts, width, height))
    _put(
        root,
        "ppt/slideMasters/_rels/slideMaster1.xml.rels",
        _master_rels(len(layouts)),
    )

    for index, name, key in layouts:
        _put(
            root,
            f"ppt/slideLayouts/slideLayout{index}.xml",
            _slide_layout(theme, name, key, width, height),
        )
        _put(
            root,
            f"ppt/slideLayouts/_rels/slideLayout{index}.xml.rels",
            _layout_rels(),
        )


def _put(root: str, rel: str, content: str) -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _zip_package(root: str, out_path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # [Content_Types].xml must be first in the archive: some consumers read
        # it before the central directory.
        first = "[Content_Types].xml"
        zf.write(os.path.join(root, first), first)
        for base, _, files in os.walk(root):
            for name in sorted(files):
                full = os.path.join(base, name)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                if rel == first:
                    continue
                zf.write(full, rel)


# --------------------------------------------------------------------------- #
# Package plumbing
# --------------------------------------------------------------------------- #


def _content_types(n_layouts: int) -> str:
    overrides = "".join(
        f'<Override PartName="/ppt/slideLayouts/slideLayout{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'presentationml.slideLayout+xml"/>'
        for i in range(1, n_layouts + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        '<Default Extension="jpeg" ContentType="image/jpeg"/>'
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
        f"{overrides}"
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
        '<Override PartName="/ppt/presProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presProps+xml"/>'
        '<Override PartName="/ppt/viewProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.viewProps+xml"/>'
        '<Override PartName="/ppt/tableStyles.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.tableStyles+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        "</Types>"
    )


def _root_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        "</Relationships>"
    )


def _core_props(theme: Theme) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{_esc(theme.name)}</dc:title>"
        f"<dc:description>{_esc(theme.description)}</dc:description>"
        "</cp:coreProperties>"
    )


def _app_props(theme: Theme) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        "<Application>Rostrum</Application><PresentationFormat>Widescreen</PresentationFormat>"
        "</Properties>"
    )


def _presentation(layouts, width: int, height: int) -> str:
    # Layout ids belong in the slide master, not here: presentation.xml lists
    # masters only.
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" saveSubsetFonts="1">'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        f'<p:sldSz cx="{width}" cy="{height}"/>'
        f'<p:notesSz cx="{height}" cy="{width}"/>'
        "</p:presentation>"
    )


def _presentation_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/presProps" Target="presProps.xml"/>'
        '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/viewProps" Target="viewProps.xml"/>'
        '<Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/tableStyles" Target="tableStyles.xml"/>'
        "</Relationships>"
    )


def _pres_props() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:presentationPr xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>'
    )


def _view_props() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:viewPr xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>'
    )


def _table_styles(theme: Theme) -> str:
    """A table style using the theme accent for the header row.

    Tables are common in results sections and PowerPoint's default style is a
    heavy blue that clashes with every theme here.
    """
    p = theme.palette
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<a:tblStyleLst xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'def="{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}">'
        '<a:tblStyle styleId="{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}" styleName="Rostrum">'
        f'<a:wholeTbl><a:tcTxStyle><a:font><a:latin typeface="+mn-lt"/></a:font>'
        f'<a:srgbClr val="{p.body}"/></a:tcTxStyle>'
        f'<a:tcStyle><a:tcBdr><a:top><a:ln w="9525"><a:solidFill><a:srgbClr val="{p.rule}"/></a:solidFill></a:ln></a:top>'
        f'<a:bottom><a:ln w="9525"><a:solidFill><a:srgbClr val="{p.rule}"/></a:solidFill></a:ln></a:bottom></a:tcBdr>'
        '<a:fill><a:noFill/></a:fill></a:tcStyle></a:wholeTbl>'
        f'<a:firstRow><a:tcTxStyle b="on"><a:font><a:latin typeface="+mj-lt"/></a:font>'
        f'<a:srgbClr val="{p.background}"/></a:tcTxStyle>'
        f'<a:tcStyle><a:tcBdr/><a:fill><a:solidFill><a:srgbClr val="{p.primary}"/></a:solidFill></a:fill>'
        "</a:tcStyle></a:firstRow>"
        "</a:tblStyle></a:tblStyleLst>"
    )


# --------------------------------------------------------------------------- #
# Theme part: colours and fonts
# --------------------------------------------------------------------------- #


def _theme_part(theme: Theme) -> str:
    p = theme.palette
    ts = theme.type_scale

    # dk1/lt1 are text/background; PowerPoint maps "Text 1" and "Background 1" to
    # them, so getting these right is what makes the whole deck obey the theme.
    clr = (
        "<a:clrScheme name=\"Rostrum\">"
        f'<a:dk1><a:srgbClr val="{p.body}"/></a:dk1>'
        f'<a:lt1><a:srgbClr val="{p.background}"/></a:lt1>'
        f'<a:dk2><a:srgbClr val="{p.primary}"/></a:dk2>'
        f'<a:lt2><a:srgbClr val="{p.band or p.background}"/></a:lt2>'
        f'<a:accent1><a:srgbClr val="{p.primary}"/></a:accent1>'
        f'<a:accent2><a:srgbClr val="{p.accent}"/></a:accent2>'
        f'<a:accent3><a:srgbClr val="{p.muted}"/></a:accent3>'
        f'<a:accent4><a:srgbClr val="{p.rule}"/></a:accent4>'
        f'<a:accent5><a:srgbClr val="{p.primary}"/></a:accent5>'
        f'<a:accent6><a:srgbClr val="{p.accent}"/></a:accent6>'
        f'<a:hlink><a:srgbClr val="{p.accent}"/></a:hlink>'
        f'<a:folHlink><a:srgbClr val="{p.muted}"/></a:folHlink>'
        "</a:clrScheme>"
    )

    # script="Hans" is what actually governs Chinese glyph selection. Setting
    # only the latin typeface leaves CJK text at the application default, which
    # on many systems is a serif face that projects badly.
    def font_block(tag: str, latin: str, cjk: str) -> str:
        return (
            f"<a:{tag}>"
            f'<a:latin typeface="{_esc(latin)}"/>'
            f'<a:ea typeface="{_esc(cjk)}"/>'
            '<a:cs typeface=""/>'
            f'<a:font script="Hans" typeface="{_esc(cjk)}"/>'
            f'<a:font script="Hant" typeface="{_esc(cjk)}"/>'
            f'<a:font script="Jpan" typeface="{_esc(cjk)}"/>'
            f"</a:{tag}>"
        )

    fonts = (
        '<a:fontScheme name="Rostrum">'
        + font_block("majorFont", ts.latin_title_font, ts.title_font)
        + font_block("minorFont", ts.latin_body_font, ts.body_font)
        + "</a:fontScheme>"
    )

    # Flat fills only. Gradients and shadows are what make a generated deck look
    # like a 2007 template; academic guidance is unanimously against them.
    fmt = (
        '<a:fmtScheme name="Rostrum">'
        "<a:fillStyleLst>"
        '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:solidFill><a:schemeClr val="phClr"><a:tint val="60000"/></a:schemeClr></a:solidFill>'
        '<a:solidFill><a:schemeClr val="phClr"><a:shade val="80000"/></a:schemeClr></a:solidFill>'
        "</a:fillStyleLst>"
        "<a:lnStyleLst>"
        '<a:ln w="9525" cap="flat"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln>'
        '<a:ln w="19050" cap="flat"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln>'
        '<a:ln w="28575" cap="flat"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln>'
        "</a:lnStyleLst>"
        "<a:effectStyleLst>"
        "<a:effectStyle><a:effectLst/></a:effectStyle>"
        "<a:effectStyle><a:effectLst/></a:effectStyle>"
        "<a:effectStyle><a:effectLst/></a:effectStyle>"
        "</a:effectStyleLst>"
        "<a:bgFillStyleLst>"
        '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        "</a:bgFillStyleLst>"
        "</a:fmtScheme>"
    )

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Rostrum">'
        f"<a:themeElements>{clr}{fonts}{fmt}</a:themeElements>"
        "<a:objectDefaults/><a:extraClrSchemeLst/>"
        "</a:theme>"
    )


# --------------------------------------------------------------------------- #
# Slide master
# --------------------------------------------------------------------------- #


def _slide_master(theme: Theme, layouts, width: int, height: int) -> str:
    g = theme.geometry
    ts = theme.type_scale
    layout_ids = "".join(
        f'<p:sldLayoutId id="{2147483660 + i}" r:id="rId{i}"/>' for i, _, _ in layouts
    )

    title_box = _box(g.margin_left, g.margin_top, g.body_width(), g.title_height, width, height)
    body_box = _box(g.margin_left, g.body_top(), g.body_width(), g.body_height(), width, height)

    shapes = (
        _ph_shape("Title Placeholder 1", 2, "title", None, title_box, theme, ts.title, "title")
        + _ph_shape("Text Placeholder 2", 3, "body", 1, body_box, theme, ts.body, "body")
    )

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        "<p:cSld>"
        f"<p:bg><p:bgPr><a:solidFill><a:srgbClr val=\"{theme.palette.background}\"/></a:solidFill>"
        "<a:effectLst/></p:bgPr></p:bg>"
        "<p:spTree>"
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        "<p:grpSpPr><a:xfrm><a:off x=\"0\" y=\"0\"/><a:ext cx=\"0\" cy=\"0\"/>"
        "<a:chOff x=\"0\" y=\"0\"/><a:chExt cx=\"0\" cy=\"0\"/></a:xfrm></p:grpSpPr>"
        f"{shapes}"
        "</p:spTree></p:cSld>"
        f"<p:clrMap bg1=\"lt1\" tx1=\"dk1\" bg2=\"lt2\" tx2=\"dk2\" accent1=\"accent1\" "
        "accent2=\"accent2\" accent3=\"accent3\" accent4=\"accent4\" accent5=\"accent5\" "
        "accent6=\"accent6\" hlink=\"hlink\" folHlink=\"folHlink\"/>"
        f"<p:sldLayoutIdLst>{layout_ids}</p:sldLayoutIdLst>"
        f"{_txt_styles(theme)}"
        "</p:sldMaster>"
    )


def _master_rels(n_layouts: int) -> str:
    rels = "".join(
        f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout{i}.xml"/>'
        for i in range(1, n_layouts + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{rels}"
        f'<Relationship Id="rId{n_layouts + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>'
        "</Relationships>"
    )


def _layout_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>'
        "</Relationships>"
    )


def _txt_styles(theme: Theme) -> str:
    """Master text styles: the type scale, expressed once.

    Defining sizes here rather than per shape is what lets the measurement layer
    read a real declared size, and what lets a user restyle the whole deck from
    the master.
    """
    ts = theme.type_scale
    p = theme.palette

    def lvl(i: int, size: float, colour: str, font: str, *, bullet: bool, space: int = 600) -> str:
        char = (
            f'<a:defRPr sz="{int(size * 100)}">'
            f'<a:solidFill><a:srgbClr val="{colour}"/></a:solidFill>'
            f'<a:latin typeface="{"+mj-lt" if font == "major" else "+mn-lt"}"/>'
            f'<a:ea typeface="{"+mj-ea" if font == "major" else "+mn-ea"}"/>'
            "</a:defRPr>"
        )
        marker = (
            '<a:buChar char="\u2022"/>'
            if bullet
            else "<a:buNone/>"
        )
        indent = 0 if not bullet else -228600
        margin = 0 if not bullet else 285750 * (i + 1)
        return (
            f'<a:lvl{i + 1}pPr marL="{margin}" indent="{indent}" algn="l">'
            f'<a:lnSpc><a:spcPct val="112000"/></a:lnSpc>'
            f'<a:spcBef><a:spcPts val="{space}"/></a:spcBef>'
            f"{marker}{char}</a:lvl{i + 1}pPr>"
        )

    title_style = (
        "<p:titleStyle>"
        + lvl(0, ts.title, p.primary, "major", bullet=False, space=0)
        + "</p:titleStyle>"
    )
    body_style = (
        "<p:bodyStyle>"
        + lvl(0, ts.body, p.body, "minor", bullet=True, space=700)
        + lvl(1, ts.sub, p.body, "minor", bullet=True, space=500)
        + lvl(2, ts.caption, p.muted, "minor", bullet=True, space=400)
        + "</p:bodyStyle>"
    )
    other = (
        "<p:otherStyle>"
        + lvl(0, ts.body, p.body, "minor", bullet=False)
        + "</p:otherStyle>"
    )
    return title_style + body_style + other


# --------------------------------------------------------------------------- #
# Slide layouts
# --------------------------------------------------------------------------- #


def _slide_layout(theme: Theme, name: str, key: str, width: int, height: int) -> str:
    builder = _BUILDERS[key]
    shapes, layout_type, show_master = builder(theme, width, height)

    bg = ""
    if key == "section" and theme.section_style == "band":
        bg = (
            f'<p:bg><p:bgPr><a:solidFill><a:srgbClr val="{theme.palette.band or theme.palette.background}"/>'
            "</a:solidFill><a:effectLst/></p:bgPr></p:bg>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        f'type="{layout_type}" preserve="1" showMasterSp="{1 if show_master else 0}">'
        f'<p:cSld name="{_esc(name)}">{bg}'
        "<p:spTree>"
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        f"{shapes}"
        "</p:spTree></p:cSld>"
        "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>"
        "</p:sldLayout>"
    )


def _cover(theme: Theme, w: int, h: int):
    g, ts, p = theme.geometry, theme.type_scale, theme.palette
    # A cover earns a bolder treatment than content pages: a wide accent rule and
    # generous space, since it is on screen while the presenter is introduced.
    shapes = _rect(
        g.margin_left, 0.40, 0.10, 0.006, p.accent, w, h, name="Cover Rule"
    )
    shapes += _ph_shape(
        "Title 1", 2, "ctrTitle", None,
        _box(g.margin_left, 0.26, 1 - g.margin_left - g.margin_right, 0.14, w, h),
        theme, ts.deck_title, "title",
    )
    shapes += _ph_shape(
        "Subtitle 2", 3, "subTitle", 1,
        _box(g.margin_left, 0.44, 1 - g.margin_left - g.margin_right, 0.22, w, h),
        theme, ts.sub, "subtitle", colour=p.muted, bullet=False,
    )
    return shapes, "title", False


def _section(theme: Theme, w: int, h: int):
    g, ts, p = theme.geometry, theme.type_scale, theme.palette
    shapes = ""
    if theme.section_style == "number":
        # A tall accent stroke to the left of the title. A numeral would be
        # better, but the number is only known at render time and a placeholder
        # digit in the layout would be worse than none.
        shapes += _rect(
            g.margin_left, 0.40, 0.006, 0.20, p.accent, w, h, name="Section Stroke"
        )
    shapes += _ph_shape(
        "Title 1", 2, "title", None,
        _box(g.margin_left + 0.032, 0.40, 1 - g.margin_left - g.margin_right - 0.032,
             0.20, w, h),
        theme, ts.section, "title",
    )
    if theme.section_style == "band":
        shapes += _rect(
            g.margin_left, 0.63, 0.08, 0.005, p.accent, w, h, name="Section Rule"
        )
    return shapes, "secHead", False


def _content(theme: Theme, w: int, h: int):
    return _titled_body(theme, w, h, "obj", cols=1)


def _agenda(theme: Theme, w: int, h: int):
    """An agenda reads as a list, so it starts at the top rather than centred.

    Centring works for two or three bullets of prose; a table of contents with
    five entries centred in a tall box looks accidental.
    """
    g, ts = theme.geometry, theme.type_scale
    shapes = _title_decoration(theme, w, h)
    shapes += _title_ph(theme, w, h)
    shapes += _ph_shape(
        "Text Placeholder 2", 3, "body", 1,
        _box(g.margin_left + 0.02, g.body_top(), g.body_width() - 0.02,
             g.body_height(), w, h),
        theme, ts.body, "body", anchor="t",
    )
    return shapes, "obj", True


def _summary(theme: Theme, w: int, h: int):
    return _titled_body(theme, w, h, "obj", cols=1)


def _equation(theme: Theme, w: int, h: int):
    return _titled_body(theme, w, h, "obj", cols=1)


def _backup(theme: Theme, w: int, h: int):
    return _titled_body(theme, w, h, "obj", cols=1)


def _two_column(theme: Theme, w: int, h: int):
    return _titled_body(theme, w, h, "twoObj", cols=2)


def _text_figure(theme: Theme, w: int, h: int):
    """Text on the left, figure on the right.

    The text column is narrower than the figure: a diagram needs area to be
    legible, and the text beside it is a takeaway line, not a paragraph.
    """
    g, ts = theme.geometry, theme.type_scale
    shapes = _title_decoration(theme, w, h)
    shapes += _title_ph(theme, w, h)

    top = g.body_top()
    height_frac = g.body_height()
    total = g.body_width()
    gap = 0.03
    text_w = (total - gap) * 0.42
    fig_w = (total - gap) * 0.58

    shapes += _ph_shape(
        "Text Placeholder 2", 3, "body", 1,
        _box(g.margin_left, top, text_w, height_frac, w, h),
        theme, ts.body, "body",
    )
    shapes += _ph_shape(
        "Picture Placeholder 3", 4, "pic", 2,
        _box(g.margin_left + text_w + gap, top, fig_w, height_frac, w, h),
        theme, ts.caption, "figure",
    )
    return shapes, "objTx", True


def _big_figure(theme: Theme, w: int, h: int):
    """A full-width figure under the title, with a caption line beneath it."""
    g, ts, p = theme.geometry, theme.type_scale, theme.palette
    shapes = _title_decoration(theme, w, h)
    shapes += _title_ph(theme, w, h)

    top = g.body_top()
    caption_h = 0.06
    fig_h = g.body_height() - caption_h - 0.015

    shapes += _ph_shape(
        "Picture Placeholder 2", 3, "pic", 1,
        _box(g.margin_left, top, g.body_width(), fig_h, w, h),
        theme, ts.caption, "figure",
    )
    shapes += _ph_shape(
        "Text Placeholder 3", 4, "body", 2,
        _box(g.margin_left, top + fig_h + 0.015, g.body_width(), caption_h, w, h),
        theme, ts.caption, "body", colour=p.muted, bullet=False,
    )
    return shapes, "picTx", True


def _table(theme: Theme, w: int, h: int):
    g, ts = theme.geometry, theme.type_scale
    shapes = _title_decoration(theme, w, h)
    shapes += _title_ph(theme, w, h)
    shapes += _ph_shape(
        "Table Placeholder 2", 3, "tbl", 1,
        _box(g.margin_left, g.body_top(), g.body_width(), g.body_height(), w, h),
        theme, ts.body, "body",
    )
    return shapes, "tbl", True


def _thanks(theme: Theme, w: int, h: int):
    g, ts, p = theme.geometry, theme.type_scale, theme.palette
    shapes = _rect(g.margin_left, 0.47, 0.08, 0.005, p.accent, w, h, name="Rule")
    shapes += _ph_shape(
        "Title 1", 2, "title", None,
        _box(g.margin_left, 0.36, 1 - g.margin_left - g.margin_right, 0.11, w, h),
        theme, ts.section, "title",
    )
    shapes += _ph_shape(
        "Text Placeholder 2", 3, "body", 1,
        _box(g.margin_left, 0.52, 1 - g.margin_left - g.margin_right, 0.16, w, h),
        theme, ts.sub, "body", colour=p.muted, bullet=False,
    )
    return shapes, "secHead", False


def _titled_body(theme: Theme, w: int, h: int, layout_type: str, *, cols: int):
    g, ts = theme.geometry, theme.type_scale
    shapes = _title_decoration(theme, w, h)
    shapes += _title_ph(theme, w, h)

    top = g.body_top()
    height_frac = g.body_height()
    if cols == 1:
        shapes += _ph_shape(
            "Text Placeholder 2", 3, "body", 1,
            _box(g.margin_left, top, g.body_width(), height_frac, w, h),
            theme, ts.body, "body",
        )
    else:
        gap = 0.035
        col_w = (g.body_width() - gap) / 2
        for i in range(2):
            shapes += _ph_shape(
                f"Text Placeholder {i + 2}", 3 + i, "body", i + 1,
                _box(g.margin_left + i * (col_w + gap), top, col_w, height_frac, w, h),
                theme, ts.body, "body",
            )
    return shapes, layout_type, True


def _title_ph(theme: Theme, w: int, h: int) -> str:
    g, ts = theme.geometry, theme.type_scale
    # The title box excludes the rule beneath it, so measured capacity reflects
    # the text area rather than the decoration.
    text_h = g.title_height * 0.78
    return _ph_shape(
        "Title 1", 2, "title", None,
        _box(g.margin_left, g.margin_top, g.body_width(), text_h, w, h),
        theme, ts.title, "title",
    )


def _title_decoration(theme: Theme, w: int, h: int) -> str:
    """The rule, bar or band that anchors every content page at one height.

    This is the single largest contributor to a deck looking designed: a
    consistent horizontal anchor turns a sequence of slides into one document.
    """
    g, p = theme.geometry, theme.palette
    y = g.margin_top + g.title_height * 0.86

    if theme.title_style == "rule":
        return _rect(g.margin_left, y, g.body_width(), 0.0035, p.rule, w, h, name="Title Rule")
    if theme.title_style == "accent_bar":
        # A short accent segment over a full-width hairline: more visual interest
        # than a plain rule, still only one accent colour.
        return (
            _rect(g.margin_left, y, g.body_width(), 0.0025, p.rule, w, h, name="Title Rule")
            + _rect(g.margin_left, y - 0.0015, 0.055, 0.0055, p.accent, w, h, name="Accent Bar")
        )
    if theme.title_style == "band":
        return _rect(
            0.0, 0.0, 1.0, g.margin_top + g.title_height,
            p.band or p.background, w, h, name="Title Band",
        )
    return ""


_BUILDERS = {
    "cover": _cover,
    "agenda": _agenda,
    "section": _section,
    "content": _content,
    "text_figure": _text_figure,
    "big_figure": _big_figure,
    "two_column": _two_column,
    "table": _table,
    "equation": _equation,
    "summary": _summary,
    "thanks": _thanks,
    "backup": _backup,
}


# --------------------------------------------------------------------------- #
# Shape construction
# --------------------------------------------------------------------------- #


def _box(x: float, y: float, cw: float, ch: float, w: int, h: int) -> tuple[int, int, int, int]:
    return (int(x * w), int(y * h), int(cw * w), int(ch * h))


def _ph_shape(
    name: str,
    shape_id: int,
    ph_type: str,
    idx: int | None,
    box: tuple[int, int, int, int],
    theme: Theme,
    size: float,
    kind: str,
    *,
    colour: str | None = None,
    bullet: bool | None = None,
    anchor: str | None = None,
) -> str:
    """A placeholder shape carrying an explicit size, so measurement is exact.

    Sizes are declared on the shape as well as in the master text styles. The
    measurement layer prefers a declared size and only derives one when absent;
    declaring it here removes that guess for built-in themes.
    """
    x, y, cx, cy = box
    p = theme.palette
    colour = colour or (p.primary if kind == "title" else p.body)
    font_ref = "+mj-lt" if kind == "title" else "+mn-lt"
    ea_ref = "+mj-ea" if kind == "title" else "+mn-ea"

    if bullet is None:
        bullet = kind == "body"
    marker = '<a:buChar char="\u2022"/>' if bullet else "<a:buNone/>"
    marL = 285750 if bullet else 0
    indent = -228600 if bullet else 0

    run_props = (
        f'<a:defRPr sz="{int(size * 100)}">'
        f'<a:solidFill><a:srgbClr val="{colour}"/></a:solidFill>'
        f'<a:latin typeface="{font_ref}"/><a:ea typeface="{ea_ref}"/>'
        "</a:defRPr>"
    )

    idx_attr = f' idx="{idx}"' if idx is not None else ""

    # Vertical anchoring, which is what stops a slide looking top-heavy.
    #
    # A duration-aware deck deliberately puts *few* bullets on a page, so
    # top-anchored body text leaves the lower two-thirds empty and the slide
    # reads as unfinished -- visible immediately in a rendered contact sheet.
    # Centring the body block distributes that whitespace above and below.
    # Titles are centred within their own band; figures are centred so an image
    # that does not fill its box sits sensibly.
    if anchor is None:
        anchor = "ctr"
        if kind in ("table",):
            # Tables grow downward from a fixed top edge; centring one fights
            # the row heights the renderer computes.
            anchor = "t"

    return (
        "<p:sp>"
        f'<p:nvSpPr><p:cNvPr id="{shape_id}" name="{_esc(name)}"/>'
        f"<p:cNvSpPr><a:spLocks noGrp=\"1\"/></p:cNvSpPr>"
        f'<p:nvPr><p:ph type="{ph_type}"{idx_attr}/></p:nvPr></p:nvSpPr>'
        "<p:spPr>"
        f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        "</p:spPr>"
        "<p:txBody>"
        f'<a:bodyPr anchor="{anchor}" wrap="square" lIns="0" tIns="45720" rIns="0" bIns="45720">'
        "<a:normAutofit/></a:bodyPr>"
        "<a:lstStyle>"
        f'<a:lvl1pPr marL="{marL}" indent="{indent}">'
        f'<a:lnSpc><a:spcPct val="112000"/></a:lnSpc>{marker}'
        f"{run_props}"
        "</a:lvl1pPr>"
        "</a:lstStyle>"
        # The size is repeated on the empty paragraph's own pPr. A consumer
        # reading paragraph properties -- which is what python-pptx and hence our
        # own measurement layer does -- cannot see a shape-level lstStyle, and
        # would fall back to PowerPoint's 44pt default for titles. That produced
        # capacities measured against the wrong size in the first build.
        f'<a:p><a:pPr marL="{marL}" indent="{indent}">'
        f'<a:lnSpc><a:spcPct val="112000"/></a:lnSpc>{marker}'
        f"{run_props}"
        "</a:pPr>"
        f'<a:endParaRPr sz="{int(size * 100)}">'
        f'<a:solidFill><a:srgbClr val="{colour}"/></a:solidFill>'
        f'<a:latin typeface="{font_ref}"/><a:ea typeface="{ea_ref}"/>'
        "</a:endParaRPr></a:p>"
        "</p:txBody></p:sp>"
    )


def _rect(
    x: float, y: float, cw: float, ch: float, colour: str, w: int, h: int, *, name: str
) -> str:
    """A flat filled rectangle: rules, bars and bands."""
    ox, oy, cx, cy = _box(x, y, cw, ch, w, h)
    return (
        "<p:sp>"
        f'<p:nvSpPr><p:cNvPr id="{abs(hash(name)) % 900 + 90}" name="{_esc(name)}"/>'
        "<p:cNvSpPr/><p:nvPr/></p:nvSpPr>"
        "<p:spPr>"
        f'<a:xfrm><a:off x="{ox}" y="{oy}"/><a:ext cx="{max(cx, 1)}" cy="{max(cy, 1)}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        f'<a:solidFill><a:srgbClr val="{colour}"/></a:solidFill>'
        "<a:ln><a:noFill/></a:ln>"
        "</p:spPr>"
        "<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr/></a:p></p:txBody>"
        "</p:sp>"
    )


def _text_box(
    x: float, y: float, cw: float, ch: float, w: int, h: int, *,
    size: float, colour: str, font: str, text: str, name: str,
) -> str:
    """A static text box, used for decoration such as a section numeral."""
    ox, oy, cx, cy = _box(x, y, cw, ch, w, h)
    ref = "+mj-lt" if font == "major" else "+mn-lt"
    ea = "+mj-ea" if font == "major" else "+mn-ea"
    body = (
        f'<a:r><a:rPr lang="zh-CN" sz="{int(size * 100)}">'
        f'<a:solidFill><a:srgbClr val="{colour}"/></a:solidFill>'
        f'<a:latin typeface="{ref}"/><a:ea typeface="{ea}"/></a:rPr>'
        f"<a:t>{_esc(text)}</a:t></a:r>"
        if text
        else "<a:endParaRPr/>"
    )
    return (
        "<p:sp>"
        f'<p:nvSpPr><p:cNvPr id="{abs(hash(name)) % 900 + 990}" name="{_esc(name)}"/>'
        "<p:cNvSpPr txBox=\"1\"/><p:nvPr/></p:nvSpPr>"
        "<p:spPr>"
        f'<a:xfrm><a:off x="{ox}" y="{oy}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/>'
        "</p:spPr>"
        '<p:txBody><a:bodyPr wrap="none" lIns="0" tIns="0" rIns="0" bIns="0"/>'
        f"<a:lstStyle/><a:p>{body}</a:p></p:txBody>"
        "</p:sp>"
    )


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
