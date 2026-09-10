"""Render the checked-in Markdown user manual (``user-manual/*.md``) at runtime.

Pages are read straight off disk and converted on each request, so the in-app
Help section can never drift from the source files. Only the app's own routes
change here — the manual itself lives in ``user-manual/`` and is edited there.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import markdown

from app.config import get_settings

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# [text](some-page.md)  or  [text](some-page.md#anchor)  — index.md's link table
_INDEX_LINK_RE = re.compile(r"\[([^\]]+)\]\(([a-z0-9][a-z0-9_-]*)\.md(?:#[^)]*)?\)")
# href="page.md"  /  href="page.md#anchor"  in rendered HTML (skips URLs with a scheme)
_MD_HREF_RE = re.compile(r'href="([a-z0-9][a-z0-9_-]*)\.md((?:#[^"]*)?)"')
# relative image / link targets that should resolve under /help/
_REL_SRC_RE = re.compile(r'src="(?!https?:|/|data:)([^"]+)"')

_MD_EXTENSIONS = ["fenced_code", "tables", "toc", "sane_lists", "attr_list"]


@dataclass(frozen=True)
class ManualPage:
    slug: str
    title: str


@dataclass(frozen=True)
class RenderedPage:
    slug: str
    title: str
    html: str


def manual_dir() -> Path:
    return get_settings().manual_dir


def _safe_slug(slug: str) -> str:
    slug = (slug or "").strip().removesuffix(".md")
    if not _SLUG_RE.match(slug):
        raise FileNotFoundError(slug)
    return slug


def page_path(slug: str) -> Path:
    path = (manual_dir() / f"{_safe_slug(slug)}.md").resolve()
    if manual_dir() not in path.parents or not path.is_file():
        raise FileNotFoundError(slug)
    return path


def _first_heading(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def list_pages() -> list[ManualPage]:
    """Navigation order + titles, taken from the link table in ``index.md`` so the
    sidebar tracks the manual. Any other ``*.md`` file present is appended."""
    base = manual_dir()
    pages: list[ManualPage] = [ManualPage("index", "Overview")]
    seen = {"index"}

    index_md = base / "index.md"
    if index_md.is_file():
        text = index_md.read_text()
        # Prefer the "Every page" Markdown table (rows start with "|"); fall back
        # to every .md link in the file if there is no such table.
        table_rows = "\n".join(ln for ln in text.splitlines() if ln.lstrip().startswith("|"))
        for title, slug in _INDEX_LINK_RE.findall(table_rows) or _INDEX_LINK_RE.findall(text):
            if slug not in seen and (base / f"{slug}.md").is_file():
                pages.append(ManualPage(slug, title.strip()))
                seen.add(slug)

    for extra in sorted(base.glob("*.md")):
        slug = extra.stem
        if slug not in seen:
            pages.append(ManualPage(slug, _first_heading(extra.read_text(), slug)))
            seen.add(slug)
    return pages


def _rewrite_links(html: str) -> str:
    html = _MD_HREF_RE.sub(r'href="/help/\1\2"', html)
    html = _REL_SRC_RE.sub(r'src="/help/\1"', html)
    return html


def render_page(slug: str) -> RenderedPage:
    slug = _safe_slug(slug)
    text = page_path(slug).read_text()
    md = markdown.Markdown(extensions=_MD_EXTENSIONS, output_format="html")
    html = _rewrite_links(md.convert(text))
    title = _first_heading(text, slug)
    if slug == "index":
        title = "User manual"
    return RenderedPage(slug=slug, title=title, html=html)


def _asset_root() -> Path:
    return (manual_dir() / "assets").resolve()


def asset_path(rel: str) -> Path:
    """Resolve ``rel`` under ``user-manual/assets`` or raise ``FileNotFoundError``."""
    root = _asset_root()
    target = (root / rel).resolve()
    if root != target and root not in target.parents:
        raise FileNotFoundError(rel)
    if not target.is_file():
        raise FileNotFoundError(rel)
    return target
