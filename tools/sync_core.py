"""Vendor the platform-agnostic Felicity core from the ha_felicity repo.

The Home Assistant integration (``ha_felicity``) ships through HACS from
``custom_components/ha_felicity/``, so the shared modules cannot live in a
separate package there.  This app therefore keeps a *copy* under
``lib/felicity_core/`` and this script refreshes it.

Only modules with no ``homeassistant.*`` import are eligible — the script
re-checks that with an AST parse and refuses to copy a file that has grown
one, which is the same boundary ``ha_felicity``'s
``tests/test_core_boundary.py`` guards from the other side.

Usage::

    python tools/sync_core.py                      # copy, using the default source path
    python tools/sync_core.py --source <path>      # ... from an explicit checkout
    python tools/sync_core.py --check              # exit 1 if the copy is stale (CI)
"""
from __future__ import annotations

import argparse
import ast
import filecmp
import shutil
import sys
from pathlib import Path

#: Modules copied verbatim from ``custom_components/ha_felicity/``.
CORE_MODULES = (
    "const.py",
    "modbus_client.py",
    "type_specific.py",
    "trex_five.py",
    "trex_ten.py",
    "trex_twenty_five.py",
    "trex_fifty.py",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEST = REPO_ROOT / "lib" / "felicity_core"
DEFAULT_SOURCE = REPO_ROOT.parent / "ha_felicity" / "custom_components" / "ha_felicity"

HEADER = (
    "# Vendored verbatim from ha_felicity/custom_components/ha_felicity/.\n"
    "# Do not edit here — change it in ha_felicity and re-run tools/sync_core.py.\n"
)


def _imports_home_assistant(path: Path) -> bool:
    """True if the module imports anything from ``homeassistant``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == "homeassistant" for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "homeassistant":
                return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help=f"ha_felicity component directory (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the vendored copy differs from the source",
    )
    args = parser.parse_args()

    source: Path = args.source
    if not source.is_dir():
        print(f"error: source directory not found: {source}", file=sys.stderr)
        return 2

    DEST.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []

    for name in CORE_MODULES:
        src = source / name
        if not src.is_file():
            print(f"error: missing core module: {src}", file=sys.stderr)
            return 2
        if _imports_home_assistant(src):
            print(
                f"error: {name} imports homeassistant.* and is no longer shareable",
                file=sys.stderr,
            )
            return 2

        dst = DEST / name
        if args.check:
            if not dst.is_file() or not filecmp.cmp(src, dst, shallow=False):
                stale.append(name)
            continue

        shutil.copyfile(src, dst)
        print(f"copied {name}")

    init = DEST / "__init__.py"
    init_body = HEADER + '"""Vendored copy of the ha_felicity platform-agnostic core."""\n'
    if args.check:
        if not init.is_file() or init.read_text(encoding="utf-8") != init_body:
            stale.append("__init__.py")
        if stale:
            print("vendored core is stale: " + ", ".join(stale), file=sys.stderr)
            print("run: python tools/sync_core.py", file=sys.stderr)
            return 1
        print("vendored core is up to date")
        return 0

    init.write_text(init_body, encoding="utf-8")
    print(f"synced {len(CORE_MODULES)} modules into {DEST.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
