"""Build the styled HTML user manual from the canonical Markdown source."""

# The embedded HTML template keeps its visual structure readable as HTML.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import html
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "user-manual.md"
TARGET = ROOT / "docs" / "user-manual.html"


@dataclass
class Section:
    title: str
    lanes: str
    lines: list[str] = field(default_factory=list)
    anchor: str | None = None

    @property
    def slug(self) -> str:
        value = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")
        return value or "section"


def render_inline(value: str) -> str:
    """Render the small inline Markdown subset used by the manual."""

    code_values: list[str] = []

    def keep_code(match: re.Match[str]) -> str:
        code_values.append(html.escape(match.group(1), quote=False))
        return f"\x00CODE{len(code_values) - 1}\x00"

    rendered = re.sub(r"`([^`]+)`", keep_code, value)
    rendered = html.escape(rendered, quote=False)
    rendered = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda match: f'<a href="{html.escape(match.group(2), quote=True)}">{match.group(1)}</a>',
        rendered,
    )
    rendered = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", rendered)
    for index, code_value in enumerate(code_values):
        rendered = rendered.replace(f"\x00CODE{index}\x00", f"<code>{code_value}</code>")
    return rendered


def table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def is_table_separator(line: str) -> bool:
    cells = table_cells(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def render_markdown(lines: list[str]) -> str:
    """Render the block Markdown subset used by docs/user-manual.md."""

    output: list[str] = []
    paragraph: list[str] = []
    index = 0

    def flush_paragraph() -> None:
        if paragraph:
            output.append(f"<p>{render_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush_paragraph()
            language = stripped[3:].strip()
            code: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            language_class = f' class="language-{html.escape(language)}"' if language else ""
            output.append(
                f"<pre><code{language_class}>{html.escape(chr(10).join(code), quote=False)}</code></pre>"
            )
            index += 1
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            quote_lines: list[str] = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                quote_lines.append(lines[index].lstrip()[1:].strip())
                index += 1
            quote = render_inline(" ".join(part for part in quote_lines if part))
            critical = " is-crit" if "Safety notice" in quote else ""
            output.append(f'<aside class="note{critical}">{quote}</aside>')
            continue

        if (
            stripped.startswith("|")
            and index + 1 < len(lines)
            and is_table_separator(lines[index + 1])
        ):
            flush_paragraph()
            headers = table_cells(stripped)
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(table_cells(lines[index]))
                index += 1
            head = "".join(f"<th>{render_inline(cell)}</th>" for cell in headers)
            body = "".join(
                "<tr>" + "".join(f"<td>{render_inline(cell)}</td>" for cell in row) + "</tr>"
                for row in rows
            )
            output.append(
                '<div class="tablewrap"><table><thead><tr>'
                f"{head}</tr></thead><tbody>{body}</tbody></table></div>"
            )
            continue

        heading = re.match(r"^(#{3,4})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            output.append(f"<h{level}>{render_inline(heading.group(2))}</h{level}>")
            index += 1
            continue

        list_match = re.match(r"^[-*]\s+(.+)$", stripped)
        ordered_match = re.match(r"^\d+\.\s+(.+)$", stripped)
        if list_match or ordered_match:
            flush_paragraph()
            ordered = ordered_match is not None
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            while index < len(lines):
                candidate = lines[index].strip()
                match = (
                    re.match(r"^\d+\.\s+(.+)$", candidate)
                    if ordered
                    else re.match(r"^[-*]\s+(.+)$", candidate)
                )
                if not match:
                    if candidate and items and not re.match(r"^(#{1,4}|```|>|\|)", candidate):
                        items[-1] += " " + candidate
                        index += 1
                        continue
                    break
                items.append(match.group(1))
                index += 1
            rendered_items = "".join(f"<li>{render_inline(item)}</li>" for item in items)
            output.append(f"<{tag}>{rendered_items}</{tag}>")
            continue

        if not stripped:
            flush_paragraph()
            index += 1
            continue

        paragraph.append(stripped)
        index += 1

    flush_paragraph()
    return "\n".join(output)


def parse_sections(markdown: str) -> tuple[str, list[Section]]:
    lines = markdown.splitlines()
    title = lines[0].removeprefix("# ").strip()
    sections: list[Section] = []
    current = Section("Manual status", "operator engineer agent")
    lanes = "operator engineer agent"
    pending_anchor: str | None = None
    skip_markdown_note = False

    for line in lines[1:]:
        if line.strip() == "<!-- markdown-only-note:start -->":
            skip_markdown_note = True
            continue
        if line.strip() == "<!-- markdown-only-note:end -->":
            skip_markdown_note = False
            continue
        if skip_markdown_note:
            continue
        if line.startswith("# Part I:"):
            if current.lines:
                sections.append(current)
            lanes = "operator"
            pending_anchor = "part-i-operator-guide"
            current = Section("", lanes)
            continue
        if line.startswith("# Part II:"):
            if current.lines:
                sections.append(current)
            lanes = "engineer agent"
            pending_anchor = "part-ii-engineering-and-agent-guide"
            current = Section("", lanes)
            continue
        if line.startswith("## "):
            if current.title and current.lines:
                sections.append(current)
            current = Section(line[3:].strip(), lanes, anchor=pending_anchor)
            pending_anchor = None
            continue
        current.lines.append(line)

    if current.title and current.lines:
        sections.append(current)
    return title, sections


def metadata(markdown: str, label: str, fallback: str) -> str:
    match = re.search(rf"^- \*\*{re.escape(label)}:\*\*\s*(.+)$", markdown, re.MULTILINE)
    return match.group(1).strip() if match else fallback


def lane_label(lanes: str) -> str:
    return {
        "operator": "Operator",
        "engineer agent": "Engineer / AI agent",
        "operator engineer agent": "Everyone",
    }[lanes]


def build() -> str:
    markdown = SOURCE.read_text(encoding="utf-8")
    title, sections = parse_sections(markdown)
    version = metadata(markdown, "Manual version", "Draft")
    applies_to = metadata(markdown, "Applies to", "application version 0.1.0")
    reviewed = metadata(markdown, "Last reviewed", "not recorded")
    source_commit = re.search(r"`([0-9a-f]{7,40})`", applies_to)
    source_label = f"source commit {source_commit.group(1)}" if source_commit else applies_to

    nav = "\n".join(
        f'<a href="#{section.slug}" data-for="{section.slug}">'
        f'<span class="n">{number:02d}</span>{html.escape(section.title)}</a>'
        for number, section in enumerate(sections)
    )
    content = []
    for number, section in enumerate(sections):
        anchor = f'<span id="{section.anchor}"></span>' if section.anchor else ""
        content.append(
            f'<section id="{section.slug}" data-lanes="{section.lanes}">\n'
            f"{anchor}\n"
            '<div class="shead">'
            f'<span class="num">{number:02d}</span><h2>{html.escape(section.title)}</h2>'
            "</div>\n"
            '<div class="lanes"><article class="lane" '
            f'data-lane="{section.lanes}"><span class="chip">{lane_label(section.lanes)}</span>\n'
            f"{render_markdown(section.lines)}\n"
            "</article></div>\n"
            "</section>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="description" content="Safety-first operator, engineer, and AI-agent manual for Algorithmic Crypto Trader." />
  <title>{html.escape(title)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,400..700&amp;family=JetBrains+Mono:wght@400;500;700&amp;family=Source+Serif+4:opsz,wght@8..60,400..600&amp;display=swap" />
  <link rel="stylesheet" href="user-manual.css" />
</head>
<body>
<div class="page" id="page" data-view="all">
  <aside class="rail">
    <div class="brand">
      <div class="mark" aria-hidden="true">ACT</div>
      <div class="who"><b>Algorithmic</b><span>Crypto Trader</span></div>
    </div>
    <nav id="toc" aria-label="Manual sections">
      <p class="navlabel">Contents</p>
      {nav}
    </nav>
    <div class="railfoot"><b>G.Holmes Designs</b><br />{html.escape(version)}<br />Safety first &middot; fail closed</div>
  </aside>

  <main class="main">
    <header class="masthead">
      <div class="inner">
        <p class="eyebrow"><span>Trading operations &middot; safety and recovery</span><span class="draft">{html.escape(version)}</span></p>
        <h1>Algorithmic Crypto Trader</h1>
        <p class="deck">One safety-first manual, organized for operators, engineers, and AI agents.</p>
        <dl class="stats">
          <div class="stat"><dt>Application</dt><dd>0.1.0<small>{html.escape(source_label)}</small></dd></div>
          <div class="stat"><dt>Reviewed</dt><dd>{html.escape(reviewed)}<small>verify against current source</small></dd></div>
          <div class="stat is-ok"><dt>Default mode</dt><dd>Backtest<small>no external orders</small></dd></div>
          <div class="stat is-warn"><dt>Live trading</dt><dd>Not authorized<small>later phase with separate approval</small></dd></div>
        </dl>
      </div>
    </header>

    <div class="switcher">
      <div class="inner">
        <span class="lede">Reading as</span>
        <div class="seg" role="group" aria-label="Choose an audience lane">
          <button type="button" data-set="operator" aria-pressed="false"><span class="dot" aria-hidden="true"></span>Operator <span class="ct" data-count="operator"></span></button>
          <button type="button" data-set="engineer" aria-pressed="false"><span class="dot" aria-hidden="true"></span>Engineer <span class="ct" data-count="engineer"></span></button>
          <button type="button" data-set="agent" aria-pressed="false"><span class="dot" aria-hidden="true"></span>AI agent <span class="ct" data-count="agent"></span></button>
          <button type="button" data-set="all" aria-pressed="true">All audiences <span class="ct" data-count="all"></span></button>
        </div>
      </div>
    </div>

    <div class="wrap">
      {"".join(content)}
    </div>

    <footer class="footer"><div class="inner">Generated from <code>docs/user-manual.md</code>. Keep safety claims synchronized with current source, tests, and owner-run evidence.</div></footer>
  </main>
</div>

<script>
  (function () {{
    var page = document.getElementById('page');
    var buttons = Array.prototype.slice.call(document.querySelectorAll('.seg button'));
    var lanes = Array.prototype.slice.call(document.querySelectorAll('.lane'));
    var sections = Array.prototype.slice.call(document.querySelectorAll('section[data-lanes]'));
    var navLinks = Array.prototype.slice.call(document.querySelectorAll('#toc a'));
    var counts = {{ operator: 0, engineer: 0, agent: 0, all: lanes.length }};

    lanes.forEach(function (element) {{
      element.getAttribute('data-lane').split(' ').forEach(function (lane) {{
        if (counts[lane] !== undefined) counts[lane] += 1;
      }});
    }});
    Object.keys(counts).forEach(function (lane) {{
      var target = document.querySelector('[data-count="' + lane + '"]');
      if (target) target.textContent = counts[lane];
    }});

    function apply(view) {{
      page.setAttribute('data-view', view);
      buttons.forEach(function (button) {{
        button.setAttribute('aria-pressed', String(button.getAttribute('data-set') === view));
      }});
      sections.forEach(function (section) {{
        var hasLane = view === 'all' || section.getAttribute('data-lanes').split(' ').indexOf(view) !== -1;
        navLinks.forEach(function (link) {{
          if (link.getAttribute('data-for') === section.id) link.hidden = !hasLane;
        }});
      }});
    }}

    buttons.forEach(function (button) {{
      button.addEventListener('click', function () {{ apply(button.getAttribute('data-set')); }});
    }});

    var visible = new Map();
    var observer = new IntersectionObserver(function (entries) {{
      entries.forEach(function (entry) {{
        visible.set(entry.target.id, entry.isIntersecting ? entry.intersectionRatio : 0);
      }});
      var best = null;
      var bestRatio = 0;
      visible.forEach(function (ratio, id) {{
        if (ratio > bestRatio) {{ bestRatio = ratio; best = id; }}
      }});
      navLinks.forEach(function (link) {{
        link.classList.toggle('on', best !== null && link.getAttribute('data-for') === best);
      }});
    }}, {{ rootMargin: '-70px 0px -55% 0px', threshold: [0, 0.25, 0.5, 1] }});
    sections.forEach(function (section) {{ observer.observe(section); }});
    apply('all');
  }})();
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when docs/user-manual.html is not synchronized with its Markdown source",
    )
    args = parser.parse_args()
    rendered = build()
    if args.check:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != rendered:
            raise SystemExit(
                "docs/user-manual.html is stale; run python tools/build_user_manual.py"
            )
        return
    TARGET.write_text(rendered, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
