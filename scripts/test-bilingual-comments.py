#!/usr/bin/env python3
"""Guardia: ogni blocco di commento sostanzioso deve essere bilingue (IT + EN).

Guard: every substantial comment block must be bilingual (IT + EN).

Convenzione / Convention: prima l'italiano, poi l'inglese, nello stesso blocco
(`# ...` / `// ...`) o nella stessa docstring. / Italian first, then English,
in the same block or docstring.

Euristica volutamente prudente: un blocco fallisce solo se ha parole
funzionali di UNA sola lingua (almeno 4) e nessuna dell'altra. I blocchi corti,
le direttive (`noqa`, `shellcheck`, ...) e il codice commentato sono ignorati.
Deliberately cautious heuristic: a block fails only if it has function words of
ONE language only (at least 4) and none of the other. Short blocks, directives
(`noqa`, `shellcheck`, ...) and commented-out code are ignored.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# File soggetti alla regola / files under the rule.
GLOBS = [
    "src/bravoric_stt_clipboard/*.py",
    "gnome-extension/bravoric-indicator@local/*.js",
    "gnome-extension/bravoric-indicator@local/*.mjs",
    "scripts/*.py", "scripts/*.js", "scripts/*.sh", "scripts/lib/*.mjs", "scripts/lib/*.cjs",
    "scripts/lib/*/extension.js",
    "bin/whisper-server.py", "bin/whisper-server.sh",
]
# Questo stesso file cita parole di entrambe le lingue per costruzione.
# This very file quotes words of both languages by construction.
SKIP = {"scripts/test-bilingual-comments.py"}

IT = re.compile(
    r"\b(il|lo|la|le|gli|di|del|della|dei|delle|che|per|con|non|nel|nella|sono|quando|come|"
    r"solo|una|un|si|dopo|prima|anche|questo|questa|deve|puo'|piu')\b|[àèìòù]",
    re.I)
EN = re.compile(
    r"\b(the|of|is|are|and|for|with|that|this|it|not|be|to|from|when|then|only|must|does|"
    r"which|here|there|because|without)\b", re.I)
DIRECTIVE = re.compile(r"(noqa|type:\s*ignore|pragma|pylint|shellcheck|eslint|fmt:|nosec|isort:|mypy:|@ts-)", re.I)
CODE_LIKE = re.compile(r"[{};=]\s*$|^\s*(if|for|const|let|def|return)\b")


def blocks(path: Path) -> list[tuple[int, str]]:
    """(riga, testo) dei blocchi di commento e delle docstring. / (line, text)."""
    src = path.read_text(encoding="utf-8")
    lines = src.split("\n")
    out: list[tuple[int, str]] = []
    marker = "#" if path.suffix in (".py", ".sh") else "//"
    cur: list[str] = []
    start = 0
    for i, line in enumerate(lines, 1):
        m = re.match(rf"^\s*{re.escape(marker)}(?!!)\s?(.*)$", line)
        if m:
            if not cur:
                start = i
            cur.append(m.group(1))
        elif cur:
            out.append((start, "\n".join(cur)))
            cur = []
    if cur:
        out.append((start, "\n".join(cur)))
    if path.suffix == ".py":
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node, clean=True)
                if doc:
                    out.append((getattr(node, "lineno", 1), doc))
    return out


def offenders() -> list[str]:
    bad = []
    for pattern in GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            rel = str(path.relative_to(ROOT))
            if rel in SKIP:
                continue
            for line_no, text in blocks(path):
                if DIRECTIVE.search(text) or len(text.split()) < 8:
                    continue
                code_lines = sum(1 for t in text.split("\n") if CODE_LIKE.search(t))
                if code_lines * 2 >= len(text.split("\n")):
                    continue
                it, en = len(IT.findall(text)), len(EN.findall(text))
                if (it >= 4 and en == 0) or (en >= 4 and it == 0):
                    bad.append(f"{rel}:{line_no}: {'IT' if it > en else 'EN'} only: {text[:70]!r}")
    return bad


if __name__ == "__main__":
    found = offenders()
    if found:
        print(f"FAIL: {len(found)} blocchi di commento monolingua / monolingual comment blocks")
        for f in found:
            print("  " + f)
        sys.exit(1)
    print("PASS: tutti i blocchi di commento sostanziosi sono bilingue / all substantial comment blocks are bilingual")
