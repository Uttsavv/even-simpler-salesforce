"""Render docs/llms.txt (https://llmstxt.org/) from the docs nav, the pages and the README.

    python scripts/render_llms_txt.py           # write docs/llms.txt
    python scripts/render_llms_txt.py --check   # exit 1 if docs/llms.txt is stale

The sources are the ``zensical.toml`` nav (order and sections), the H1 and first paragraph of
each page, the first paragraph of ``README.md`` and the front matter of the shipped agent
skills. Links point to raw Markdown on ``main``, so an agent reads the page source, not the
rendered site. Never edit docs/llms.txt by hand.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SKILLS = ROOT / "src" / "even_simpler_salesforce" / "skills"
TARGET = DOCS / "llms.txt"
# The home page is a template without text, the API reference is generated from docstrings, and
# the changelog includes CHANGELOG.md; none of them read well as Markdown.
SKIP = {"index.md", "reference/index.md", "changelog.md"}
# Nav sections an agent can skip when its context is short (the llms.txt "Optional" section).
OPTIONAL = {"Reference"}

_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z`])")


def _plain(text: str) -> str:
    """One line of text: Markdown links become their label, whitespace collapses."""
    return " ".join(_LINK.sub(r"\1", text).split())


def _first_paragraph(lines: list[str]) -> str:
    """The first prose paragraph: skips front matter, comments, headings, code and admonitions."""
    para: list[str] = []
    fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fence = not fence
            if para:
                break
            continue
        if fence:
            continue
        if not stripped:
            if para:
                break
            continue
        if stripped.startswith(("#", "<", "!!!", "|", ">", "---", "===", "-", "*")) and not para:
            continue
        para.append(stripped)
    return _plain(" ".join(para))


def _summary(paragraph: str, sentences: int = 2) -> str:
    return " ".join(_SENTENCE.split(paragraph)[:sentences])


def page_entry(path: str) -> tuple[str, str]:
    """``(H1, summary)`` of a docs page."""
    lines = (DOCS / path).read_text(encoding="utf-8").splitlines()
    h1 = next((ln[2:].strip() for ln in lines if ln.startswith("# ")), None)
    if h1 is None:
        raise SystemExit(f"render_llms_txt: {path} has no H1")
    body = lines[lines.index(f"# {h1}") + 1 :] if f"# {h1}" in lines else lines
    return _plain(h1.replace("`", "")), _summary(_first_paragraph(body))


def _front_matter(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    lines = text.splitlines()
    key = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if key and line.startswith(" "):
            out[key] = f"{out[key]} {line.strip()}".strip()
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        out[key] = "" if value.strip() in (">-", ">") else value.strip()
    return out


def _sections(nav: list[Any]) -> list[tuple[str, list[str]]]:
    """``(section title, [page paths])`` in nav order; top-level pages go to "Start"."""
    sections: list[tuple[str, list[str]]] = [("Start", [])]
    for item in nav:
        for title, value in item.items():
            if isinstance(value, list):
                sections.append((title, [p for sub in value for p in sub.values()]))
            else:
                sections[0][1].append(value)
    return [(t, [p for p in pages if p not in SKIP]) for t, pages in sections]


def render() -> str:
    config = tomllib.loads((ROOT / "zensical.toml").read_text(encoding="utf-8"))["project"]
    repo = config["repo_url"].removeprefix("https://github.com/").strip("/")
    raw = f"https://raw.githubusercontent.com/{repo}/main"
    site = config["site_url"].rstrip("/")
    readme = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()

    out = [
        f"# {config['site_name']}",
        "",
        f"> {_summary(_first_paragraph(readme))}",
        "",
        "- Install: `pip install even-simpler-salesforce`. Import it as "
        "`import even_simpler_salesforce as esf`.",
        "- The links below point to the Markdown source on `main`, which can be newer than the "
        "installed version. Check `esf.__version__` and the changelog if a name is missing.",
        f"- The rendered site is at {site}/ (the root redirects to `/latest/`).",
        "- The library ships agent skills that match the installed version: run "
        "`esf skills list` and `esf skills install`.",
        "",
    ]
    nav = _sections(config["nav"])
    optional = [s for s in nav if s[0] in OPTIONAL]
    for title, pages in [s for s in nav if s[0] not in OPTIONAL]:
        out += [f"## {title}", ""]
        out += [f"- [{h1}]({raw}/docs/{p}): {summary}" for p in pages for h1, summary in [page_entry(p)]]
        out.append("")
    out += ["## Agent skills", ""]
    for skill in sorted(SKILLS.glob("*/SKILL.md")):
        meta = _front_matter(skill.read_text(encoding="utf-8"))
        rel = skill.relative_to(ROOT).as_posix()
        out.append(f"- [{meta['name']}]({raw}/{rel}): {_summary(meta['description'], 1)}")
    out.append("")
    for _title, pages in optional:
        out += ["## Optional", ""]
        out += [f"- [{h1}]({raw}/docs/{p}): {summary}" for p in pages for h1, summary in [page_entry(p)]]
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if docs/llms.txt is stale")
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        if not TARGET.is_file() or TARGET.read_text(encoding="utf-8") != text:
            print("docs/llms.txt is stale: run python scripts/render_llms_txt.py", file=sys.stderr)
            return 1
        return 0
    TARGET.write_text(text, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT) if TARGET.is_relative_to(ROOT) else TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
