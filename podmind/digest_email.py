"""Render the daily digest as a readable HTML email.

Builds the email HTML directly from the structured thread data (threads +
uncategorized + slug→EpisodePage lookup) rather than from the filed markdown.
That lets `[[episodes/<slug>]]` wikilinks resolve to real titles and clickable
source links — the inbox never sees a raw slug. The filed
`wiki/digests/<date>.md` is untouched and keeps native Obsidian wikilinks.

See docs/superpowers/specs/2026-06-17-digest-email-readability-design.md.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import escape
from typing import Callable
from urllib.parse import quote

from podmind import paths
from podmind.frontmatter import EpisodePage
from podmind.threads import Thread, _badge_for


@dataclass(frozen=True)
class EpisodeMeta:
    """Display data resolved from an episode's meta.json."""
    title: str
    source_url: str | None


MetaLoader = Callable[[str, "EpisodePage | None"], EpisodeMeta]

_HOOK_PREFIX_RE = re.compile(r"^[🎧▶⚪][^—]*—\s*(.*)")


def _slug_to_title(slug: str) -> str:
    """Fallback display title when meta.json is missing: de-kebab the slug."""
    return slug.replace("-", " ").strip().capitalize() or slug


def _default_meta_loader(slug: str, ep: EpisodePage | None) -> EpisodeMeta:
    """Read raw/episodes/<raw_dir>/meta.json → title + best source URL.

    Prefers youtube_url over audio_url. Any missing file / parse error /
    absent raw_dir falls back to a slug-derived title and no link.
    """
    if ep is not None and ep.raw_dir:
        meta_path = paths.RAW_DIR / "episodes" / ep.raw_dir / "meta.json"
        try:
            m = json.loads(meta_path.read_text())
            title = (m.get("title") or "").strip() or _slug_to_title(slug)
            source = m.get("youtube_url") or m.get("audio_url") or None
            return EpisodeMeta(title=title, source_url=source)
        except (OSError, json.JSONDecodeError):
            pass
    return EpisodeMeta(title=_slug_to_title(slug), source_url=None)


def _vault_link(slug: str, vault_name: str) -> str:
    """obsidian:// deep-link to the wiki episode page (URL-encoded)."""
    return (f"obsidian://open?vault={quote(vault_name)}"
            f"&file={quote(f'wiki/episodes/{slug}')}")


def _hook_text(ep: EpisodePage | None) -> str:
    """The editorial hook sentence, with any leading badge + show-link prefix
    stripped (mirrors threads.format_threads_md)."""
    if not ep or not ep.hook:
        return ""
    m = _HOOK_PREFIX_RE.match(ep.hook)
    return (m.group(1) if m else ep.hook).strip()


# Inline styles — email clients strip <style>/<head> and choke on flexbox, so
# every rule rides on its element and colours are fixed hex (no CSS vars).
_S_BODY = ("font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
           "max-width:560px;margin:0 auto;padding:8px 4px;color:#1a1a1a;"
           "line-height:1.55")
_S_H1 = "font-size:22px;font-weight:600;margin:0 0 2px"
_S_META = "font-size:13px;color:#6b6b6b;margin:0 0 20px"
_S_H2 = "font-size:17px;font-weight:600;margin:24px 0 4px"
_S_SUMMARY = "font-size:14px;color:#444;margin:0 0 12px"
_S_LABEL = ("font-size:12px;letter-spacing:.04em;color:#9a9a9a;"
            "text-transform:uppercase;margin:24px 0 12px")
_S_EP = "padding:0 0 16px;margin:0 0 16px;border-bottom:1px solid #ededed"
_S_TITLELINE = "font-size:16px;line-height:1.4;margin:0 0 5px"
_S_TITLE_A = "color:#1a5fb4;text-decoration:none;font-weight:600"
_S_TITLE_PLAIN = "color:#1a1a1a;font-weight:600"
_S_SUB = "font-size:12px;color:#9a9a9a;margin:0 0 7px"
_S_VAULT_A = "color:#6b6b6b;text-decoration:none"
_S_HOOK = "font-size:14px;color:#444;line-height:1.6;margin:0"
_S_BADGE = "margin-right:6px"


def _episode_html(slug: str, lookup: dict[str, EpisodePage], vault_name: str,
                  meta_loader: MetaLoader,
                  source_overrides: dict[str, str] | None = None) -> str:
    """Build the HTML block for a single episode: badge, linked title, sub-line, hook."""
    ep = lookup.get(slug)
    meta = meta_loader(slug, ep)
    badge = _badge_for(ep, ep.duration_min * 60) if ep else "⚪"
    source = (source_overrides or {}).get(slug) or meta.source_url
    title = escape(meta.title)
    if source:
        title_html = (f'<a href="{escape(source)}" '
                      f'style="{_S_TITLE_A}">{title}</a>')
    else:
        title_html = f'<span style="{_S_TITLE_PLAIN}">{title}</span>'

    sub_bits: list[str] = []
    if ep and ep.show:
        sub_bits.append(escape(ep.show))
    if ep and ep.duration_min:
        sub_bits.append(f"{ep.duration_min} min")
    sub_bits.append(f'<a href="{escape(_vault_link(slug, vault_name))}" '
                    f'style="{_S_VAULT_A}">⧉ vault</a>')
    sub = " · ".join(sub_bits)

    hook = escape(_hook_text(ep))
    hook_html = f'<p style="{_S_HOOK}">{hook}</p>' if hook else ""

    return (
        f'<div style="{_S_EP}">'
        f'<p style="{_S_TITLELINE}">'
        f'<span style="{_S_BADGE}">{badge}</span>{title_html}</p>'
        f'<p style="{_S_SUB}">{sub}</p>'
        f'{hook_html}'
        f'</div>'
    )


def render_email_html(
    threads: list[Thread],
    uncategorized: list[str],
    *,
    date_str: str,
    window_label: str = "the last 24h",
    episode_lookup: dict[str, EpisodePage] | None = None,
    vault_name: str = "podcast-wiki",
    meta_loader: MetaLoader = _default_meta_loader,
    source_overrides: dict[str, str] | None = None,
) -> str:
    """Render threads + uncategorized as a complete, email-safe HTML document.

    `source_overrides` is an optional slug→URL map whose entries take precedence
    over the meta-loaded source URL (youtube_url / audio_url), giving link
    precedence: override (e.g. PocketCasts) > meta.source_url > no link.
    """
    lookup = episode_lookup or {}
    n_eps = sum(len(t.episode_slugs) for t in threads) + len(uncategorized)

    out: list[str] = [f'<div style="{_S_BODY}">']
    out.append(f'<h1 style="{_S_H1}">Threads — {escape(date_str)}</h1>')
    ep_word = "episode" if n_eps == 1 else "episodes"
    th_word = "thread" if len(threads) == 1 else "threads"
    out.append(f'<p style="{_S_META}">{n_eps} {ep_word} across {len(threads)} '
               f'{th_word} · {escape(window_label)}</p>')

    for t in sorted(threads, key=lambda th: -len(th.episode_slugs)):
        k = len(t.episode_slugs)
        out.append(f'<h2 style="{_S_H2}">{escape(t.name)} '
                   f'({k} episode{"s" if k != 1 else ""})</h2>')
        if t.summary:
            out.append(f'<p style="{_S_SUMMARY}">{escape(t.summary)}</p>')
        for slug in t.episode_slugs:
            out.append(_episode_html(slug, lookup, vault_name, meta_loader, source_overrides))

    if uncategorized:
        out.append(f'<p style="{_S_LABEL}">Uncategorized · {len(uncategorized)}</p>')
        for slug in uncategorized:
            out.append(_episode_html(slug, lookup, vault_name, meta_loader, source_overrides))

    out.append('</div>')
    return "\n".join(out)
