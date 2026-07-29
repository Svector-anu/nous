"""The viewer is an es-module graph, so a single missing file breaks the whole page
silently: the import throws, no top-level code runs, and the browser shows a dark page
with the status still reading "connecting". Nothing in the sim suite catches that, so
these tests walk the graph the way the browser does."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from src.api.server import VIEWER_INDEX, create_app

VIEWER_DIR = VIEWER_INDEX.parent

# `import x from "./y.js"`, `import "./y.js"`, and the `export ... from` form.
IMPORT_SPECIFIER = re.compile(r"""(?:import|export)\b[^;'"]*?["'](\.{0,2}/[^"']+)["']""")


def _specifiers(source: str) -> list[str]:
    return IMPORT_SPECIFIER.findall(source)


def _resolve(specifier: str, importer: Path) -> Path:
    """Absolute specifiers are served from /static, which is the viewer directory;
    relative ones resolve against the importing file, as they do in a browser."""
    if specifier.startswith("/static/"):
        return VIEWER_DIR / specifier[len("/static/") :]
    return (importer.parent / specifier).resolve()


def _walk() -> set[Path]:
    """Every module the page reaches, starting from the index."""
    pending = [
        _resolve(specifier, VIEWER_INDEX)
        for specifier in _specifiers(VIEWER_INDEX.read_text())
    ]
    seen: set[Path] = set()
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        if module.exists():
            pending.extend(_resolve(s, module) for s in _specifiers(module.read_text()))
    return seen


def test_the_index_imports_the_focus_module():
    assert _resolve("/static/focus3d.js", VIEWER_INDEX) in _walk()


def test_every_module_in_the_graph_exists_on_disk():
    """three.js ships as three.module.min.js importing a sibling three.core.min.js.
    Vendoring only the first one leaves an unresolvable import."""
    missing = sorted(str(m) for m in _walk() if not m.exists())
    assert missing == []


def test_every_module_in_the_graph_is_served():
    app = create_app()
    with TestClient(app) as client:
        for module in sorted(_walk()):
            path = f"/static/{module.relative_to(VIEWER_DIR)}"
            response = client.get(path)
            assert response.status_code == 200, f"{path} -> {response.status_code}"
            assert "javascript" in response.headers["content-type"]


def test_the_page_reports_a_script_failure_instead_of_going_dark():
    """The guard has to be a classic script: a module is deferred and would install its
    handler too late to catch its own load error."""
    html = VIEWER_INDEX.read_text()
    guard = html.index('window.addEventListener("error"')
    assert html.index("<script>") < guard < html.index('<script type="module">')
