"""Run a command and save its (colored) terminal output as an SVG "screenshot".

    uv run --project app python scripts/capture.py <name> "<title>" -- <command ...>

Real identities in the output (UPNs, display names, subscription / tenant ids)
are replaced using a local, git-ignored JSON map (default: .redact.json):

    {"jane.doe@corp.example": "alice@contoso.example", "Jane Doe": "Alice Chen"}

Images are written to docs/images/poc/<name>.svg.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.terminal_theme import MONOKAI
from rich.text import Text

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "images" / "poc"


SEPARATORS = "│┃|"


def _redact_line(line: str, real: str, fake: str) -> str:
    """Replace every occurrence, then pad before the next table border so columns stay aligned."""
    pat = re.compile(re.escape(real), re.IGNORECASE)
    pos = 0
    while m := pat.search(line, pos):
        line = line[: m.start()] + fake + line[m.end() :]
        pos = m.start() + len(fake)
        deficit = len(real) - len(fake)
        if deficit > 0:
            nxt = min((k for k in (line.find(c, pos) for c in SEPARATORS) if k >= 0), default=-1)
            if nxt >= 0:
                line = line[:nxt] + " " * deficit + line[nxt:]
    return line


def redact(text: str, mapping: dict[str, str]) -> str:
    # Placeholders should not be longer than the real value (padding can only add spaces).
    for real, fake in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        text = "\n".join(_redact_line(ln, real, fake) for ln in text.split("\n"))
    return text


def json_table(raw: str, mapping: dict[str, str]):
    """Render a JSON list of objects (e.g. `az ... -o json`) as a table, redacting each cell first."""
    from rich import box
    from rich.table import Table

    rows = json.loads(raw)
    if isinstance(rows, dict):
        rows = [rows]
    cols: list[str] = []
    for r in rows:
        cols += [k for k in r if k not in cols]
    t = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan")
    for c in cols:
        t.add_column(c)

    def cell(v) -> str:
        if v is None:
            return ""
        if isinstance(v, list | dict):
            v = ", ".join(map(str, v)) if isinstance(v, list) else json.dumps(v, ensure_ascii=False)
        return redact(str(v), mapping)

    for r in rows:
        t.add_row(*(cell(r.get(c)) for c in cols))
    return t


def render(name: str, title: str, prompt: str, output: str, *, width: int = 170, table: bool = False) -> None:
    mapping_file = Path(os.environ.get("XL_REDACT", ROOT / ".redact.json"))
    mapping = json.loads(mapping_file.read_text()) if mapping_file.exists() else {}
    con = Console(record=True, width=width, force_terminal=True, color_system="truecolor", file=open(os.devnull, "w"))
    con.print(Text("$ ", style="bold green") + Text(redact(prompt, mapping), style="bold"))
    con.print(json_table(output, mapping) if table else Text.from_ansi(redact(output.rstrip(), mapping)))
    OUT.mkdir(parents=True, exist_ok=True)
    con.save_svg(str(OUT / f"{name}.svg"), title=title, theme=MONOKAI)


def run(cmd: list[str], width: int = 170) -> str:
    env = {**os.environ, "COLUMNS": str(width), "FORCE_COLOR": "1", "TERM": "xterm-256color"}
    return subprocess.run(cmd, capture_output=True, text=True, env=env).stdout


def main() -> int:
    if "--" not in sys.argv or len(sys.argv) < 5:
        print(__doc__)
        return 2
    sep = sys.argv.index("--")
    name, title, cmd = sys.argv[1], sys.argv[2], sys.argv[sep + 1 :]
    width = int(os.environ.get("CAPTURE_WIDTH", "170"))
    render(
        name,
        title,
        os.environ.get("CAPTURE_PROMPT") or " ".join(cmd),
        run(cmd, width),
        width=width,
        table=bool(os.environ.get("CAPTURE_TABLE")),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
