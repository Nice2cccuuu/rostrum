"""Check the checked-in schemas still describe the current models.

Run with::

    PYTHONPATH=src python3 scripts/check_schemas.py

Why this exists rather than ``git diff --exit-code`` over ``schemas/``:

That check conflated two very different failures. One is real -- a model gained a
field and the exported contract was never refreshed, so a non-Python consumer
validates against a schema that no longer matches. The other is noise: pydantic
changes how it renders JSON Schema between releases, so the same models emit
``{"allOf": [{"$ref": ...}]}`` under one version and ``{"$ref": ...}`` under the
next. With ``pydantic>=2.5`` unpinned, CI installs whatever is current and the byte
comparison fails on a machine where nothing is actually wrong.

Both happened here at once. The byte diff reported 169 changed lines; exactly 12 of
them mattered -- ``Slide.dwell_locked`` and ``Block.channel_pinned``, added four
commits earlier and never exported. The signal was real and buried in noise, which
is the reliable way to train people to ignore a check.

So this compares what the contract actually promises -- which models exist, which
fields they carry, whether each is required, and the enum values -- and ignores how
pydantic chose to spell it.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO / "schemas"


def _shape(schema: dict) -> dict:
    """Reduce a JSON Schema to the promises it makes to a consumer.

    Keeps model names, field names, required-ness and enum members. Drops
    descriptions, titles, defaults and the ``$ref``/``allOf`` spelling, none of
    which change what a document must look like to validate.
    """
    shape: dict[str, dict] = {}
    for name, defn in sorted((schema.get("$defs") or {}).items()):
        if not isinstance(defn, dict):
            continue
        entry: dict = {}
        if "properties" in defn:
            entry["fields"] = sorted(defn["properties"])
            entry["required"] = sorted(defn.get("required", []))
        if "enum" in defn:
            entry["enum"] = sorted(map(str, defn["enum"]))
        if entry:
            shape[name] = entry
    if "properties" in schema:
        shape["__root__"] = {
            "fields": sorted(schema["properties"]),
            "required": sorted(schema.get("required", [])),
        }
    return shape


def main() -> int:
    if not SCHEMA_DIR.is_dir():
        print(f"no schemas/ directory at {SCHEMA_DIR}", file=sys.stderr)
        return 1

    committed = {
        p.name: json.loads(p.read_text())
        for p in SCHEMA_DIR.glob("*.schema.json")
    }
    if not committed:
        print("schemas/ contains no *.schema.json files", file=sys.stderr)
        return 1

    # Regenerate into a scratch directory so the working tree is left alone --
    # a check that mutates the thing it checks cannot be run twice.
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp)
        env_script = REPO / "scripts" / "export_schemas.py"
        result = subprocess.run(
            [sys.executable, str(env_script), "--out-dir", str(out)],
            capture_output=True,
            text=True,
            cwd=REPO,
            env={**__import__("os").environ, "PYTHONPATH": str(REPO / "src")},
        )
        if result.returncode != 0:
            print("export_schemas.py failed:", file=sys.stderr)
            print(result.stderr[-2000:], file=sys.stderr)
            return 1

        # Only compare schemas. export_schemas.py also writes example decks into
        # the same directory, and an example is data, not a contract.
        regenerated = {
            p.name: json.loads(p.read_text())
            for p in out.glob("*.schema.json")
        }

    problems: list[str] = []

    missing = set(committed) - set(regenerated)
    added = set(regenerated) - set(committed)
    for name in sorted(missing):
        problems.append(f"{name}: committed but no longer generated")
    for name in sorted(added):
        problems.append(f"{name}: generated but not committed")

    for name in sorted(set(committed) & set(regenerated)):
        want, have = _shape(regenerated[name]), _shape(committed[name])
        if want == have:
            continue
        for model in sorted(set(want) | set(have)):
            w, h = want.get(model), have.get(model)
            if w == h:
                continue
            if w is None:
                problems.append(f"{name}: model {model!r} committed but not generated")
            elif h is None:
                problems.append(f"{name}: model {model!r} generated but not committed")
            else:
                for key in ("fields", "required", "enum"):
                    gone = set(h.get(key, [])) - set(w.get(key, []))
                    new = set(w.get(key, [])) - set(h.get(key, []))
                    if new:
                        problems.append(
                            f"{name}: {model}.{key} missing from committed schema: "
                            + ", ".join(sorted(new))
                        )
                    if gone:
                        problems.append(
                            f"{name}: {model}.{key} in committed schema but no longer "
                            "in the model: " + ", ".join(sorted(gone))
                        )

    if problems:
        print("Exported schemas no longer describe the models:\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(
            "\nRegenerate with:  PYTHONPATH=src python3 scripts/export_schemas.py",
            file=sys.stderr,
        )
        return 1

    n = len(committed)
    print(f"schemas describe the current models ({n} file{'s' if n != 1 else ''})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
