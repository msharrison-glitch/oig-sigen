#!/usr/bin/env python3
"""Generate the published page from the markdown, so they cannot diverge.

They already did. An edit to the gate went into the hand-written HTML and not
into the markdown, and for most of a day the repo and the published page
disagreed about how the proposal would be decided -- while I treated them as
one document. This exists so that cannot happen again: the markdown is the
source, the page is output, and the only way to change the page is to change
the markdown.

Stdlib only, like everything else here. It implements the small subset of
markdown this document actually uses -- headings, paragraphs, bullets, one
ordered list, indented code, and inline code/bold/italic -- and nothing else.
That is deliberate: a partial markdown parser that silently mangles a
construct it does not know is worse than one that refuses. Unknown block
syntax raises rather than guessing.

    python3 docs/build_page.py            # writes the html next to the md
    python3 docs/build_page.py --out P    # or somewhere else
"""

from __future__ import annotations

import argparse
import html
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
SOURCE = HERE / "proposal-release-when-car-finishes.md"
CSS = HERE / "page.css"
FIGURE = HERE / "settlement-timeline.svg"

# Where the figure is injected. A marker in the markdown rather than a
# guess based on heading position, so moving a section cannot silently move
# the diagram away from the text that explains it.
FIGURE_MARKER = "<!--FIGURE-->"

TITLE = "The Settlement Gap"


def inline(text: str) -> str:
    """Inline spans. Escape FIRST, then mark up, so content cannot inject."""
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*(?!\*)([^*]+)\*(?!\*)", r"<em>\1</em>", out)
    return out


def convert(md: str, figure: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    para: list[str] = []

    def flush() -> None:
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]

        if line.strip() == FIGURE_MARKER:
            flush()
            out.append('<div class="fig">' + figure + "</div>")
            i += 1
        elif not line.strip():
            flush()
            i += 1
        elif line.startswith("#"):
            flush()
            level = len(line) - len(line.lstrip("#"))
            if level > 3:
                raise ValueError(f"line {i+1}: only h1-h3 are supported")
            out.append(f"<h{level}>{inline(line[level:].strip())}</h{level}>")
            i += 1
        elif line.startswith("    "):
            flush()
            block = []
            while i < len(lines) and (lines[i].startswith("    ")
                                      or not lines[i].strip()):
                if lines[i].strip():
                    block.append(lines[i][4:])
                elif i + 1 < len(lines) and lines[i + 1].startswith("    "):
                    block.append("")
                else:
                    break
                i += 1
            out.append("<pre>" + html.escape("\n".join(block)) + "</pre>")
        elif re.match(r"^\d+\. ", line):
            flush()
            items = []
            while i < len(lines) and re.match(r"^\d+\. ", lines[i]):
                item = re.sub(r"^\d+\. ", "", lines[i])
                i += 1
                while i < len(lines) and lines[i].startswith("   ") \
                        and lines[i].strip() and not re.match(r"^\d+\. ", lines[i]):
                    item += " " + lines[i].strip()
                    i += 1
                items.append(item)
            out.append('<ol class="steps">'
                       + "".join(f"<li>{inline(x)}</li>" for x in items)
                       + "</ol>")
        elif re.match(r"^[-*] ", line):
            flush()
            items = []
            while i < len(lines) and re.match(r"^[-*] ", lines[i]):
                item = lines[i][2:]
                i += 1
                while i < len(lines) and lines[i].startswith("  ") \
                        and lines[i].strip():
                    item += " " + lines[i].strip()
                    i += 1
                items.append(item)
            out.append("<ul>" + "".join(f"<li>{inline(x)}</li>"
                                        for x in items) + "</ul>")
        elif line.startswith(">") or line.startswith("```") \
                or line.startswith("|"):
            raise ValueError(f"line {i+1}: unsupported block syntax {line[:20]!r}"
                             " -- add it to build_page.py rather than letting"
                             " it render as prose")
        else:
            para.append(line.strip())
            i += 1
    flush()
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "settlement-gap.html"))
    args = ap.parse_args()

    for path in (SOURCE, CSS, FIGURE):
        if not path.exists():
            print(f"missing: {path}", file=sys.stderr)
            return 2

    md = SOURCE.read_text()
    if FIGURE_MARKER not in md:
        print(f"warning: {FIGURE_MARKER} not in the markdown; the timeline "
              "will not appear", file=sys.stderr)

    body = convert(md, FIGURE.read_text().strip())
    page = (f"<title>{html.escape(TITLE)}</title>\n"
            '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
            '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
            "family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600"
            '&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap">\n'
            f"<style>\n{CSS.read_text().strip()}\n</style>\n\n"
            f'<div class="wrap">\n{body}\n</div>\n')
    out = pathlib.Path(args.out)
    out.write_text(page)
    print(f"{out}  ({len(page)} bytes, {len(body.splitlines())} blocks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
