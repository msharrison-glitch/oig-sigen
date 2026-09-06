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


def _split_on_blank(lines: list[str]) -> list[list[str]]:
    """Group lines into paragraphs, splitting on blanks."""
    groups: list[list[str]] = [[]]
    for ln in lines:
        if ln:
            groups[-1].append(ln)
        elif groups[-1]:
            groups.append([])
    return [g for g in groups if g]


def inline(text: str) -> str:
    """Inline spans. Escape FIRST, then mark up, so content cannot inject.

    Code spans are lifted out before the emphasis passes and put back after.
    Without that, an asterisk inside backticks pairs with one outside it and
    the tags interleave: `*.log` next to *emphasis* produced
    "<code><em>.log</code> and </em>", which browsers recover from however
    they like. This is a document about CLI flags and globs, so that input is
    a matter of time rather than hypothesis.
    """
    out = html.escape(text, quote=False)
    spans: list[str] = []

    def stash(m: re.Match) -> str:
        spans.append(m.group(1))
        return f"\x00{len(spans) - 1}\x00"

    out = re.sub(r"`([^`]+)`", stash, out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*(?!\*)([^*]+)\*(?!\*)", r"<em>\1</em>", out)
    return re.sub(r"\x00(\d+)\x00",
                  lambda m: f"<code>{spans[int(m.group(1))]}</code>", out)


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
            items: list[str] = []
            # A blank line BETWEEN items is the normal markdown idiom, and
            # the first version stopped at it -- emitting one <ol> per item,
            # each restarting at 1 because page.css counter-resets per list.
            # Worse, an indented continuation after a blank fell through to
            # the code-block branch and rendered as <pre>. Look past blanks
            # for a continuation before giving up on the list.
            while i < len(lines):
                if re.match(r"^\d+\. ", lines[i]):
                    items.append(re.sub(r"^\d+\. ", "", lines[i]))
                    i += 1
                elif items and lines[i].strip() and lines[i].startswith("   "):
                    items[-1] += " " + lines[i].strip()
                    i += 1
                elif not lines[i].strip():
                    j = i
                    while j < len(lines) and not lines[j].strip():
                        j += 1
                    if j < len(lines) and (re.match(r"^\d+\. ", lines[j])
                                           or lines[j].startswith("   ")):
                        i = j
                    else:
                        break
                else:
                    break
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
                    if re.match(r"^\s+[-*] ", lines[i]):
                        raise ValueError(
                            f"line {i+1}: nested bullets are not supported."
                            " The first version silently folded them into the"
                            " parent item with the marker still visible, which"
                            " is the mangling this converter exists to refuse."
                            " Add real nesting to build_page.py, or flatten"
                            " the list in the markdown.")
                    item += " " + lines[i].strip()
                    i += 1
                items.append(item)
            out.append("<ul>" + "".join(f"<li>{inline(x)}</li>"
                                        for x in items) + "</ul>")
        elif line.startswith(">"):
            # Added when the proposal needed to quote Octopus directly. The
            # converter raised rather than rendering the ">" as prose, which
            # is what it promised to do -- this is that promise being kept
            # and then paid off, not worked around.
            flush()
            quoted: list[str] = []
            while i < len(lines) and (lines[i].startswith(">")
                                      or (quoted and not lines[i].strip()
                                          and i + 1 < len(lines)
                                          and lines[i + 1].startswith(">"))):
                if lines[i].startswith(">"):
                    quoted.append(lines[i].lstrip(">").strip())
                else:
                    quoted.append("")          # paragraph break inside a quote
                i += 1
            paras = [" ".join(g).strip() for g in
                     _split_on_blank(quoted)]
            out.append("<blockquote>"
                       + "".join(f"<p>{inline(x)}</p>" for x in paras if x)
                       + "</blockquote>")
        elif line.startswith("```") or line.startswith("|"):
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

    md = SOURCE.read_text(encoding="utf-8")
    if FIGURE_MARKER not in md:
        print(f"warning: {FIGURE_MARKER} not in the markdown; the timeline "
              "will not appear", file=sys.stderr)

    body = convert(md, FIGURE.read_text(encoding="utf-8").strip())
    page = ('<meta charset="utf-8">\n'
            f"<title>{html.escape(TITLE)}</title>\n"
            '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
            '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
            "family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600"
            '&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap">\n'
            f"<style>\n{CSS.read_text(encoding='utf-8').strip()}\n</style>\n\n"
            f'<div class="wrap">\n{body}\n</div>\n')
    out = pathlib.Path(args.out)
    out.write_text(page, encoding="utf-8")
    print(f"{out}  ({len(page)} bytes, {len(body.splitlines())} blocks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
