#!/usr/bin/env -S uv run --extra stats python
"""build_stats — generate wiki/stats.md + wiki/stats/*.png from raw/episodes data.

Draws on patterns from Spotify Wrapped, Last.fm, Strava, GitHub contributions:
- Calendar heatmap for daily activity
- Year-by-year volume bars
- Top-N tables with delta from prior period (rising/falling indicators)
- Stacked bars for topic evolution over time
- Funnel chart for ingest coverage

All charts saved as PNG into wiki/stats/. The stats.md page is regeneratable —
re-run any time after new ingests.
"""
import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

from _lib import DATA_ROOT, EPISODES_DIR, WIKI_DIR  # noqa: E402

ROOT = DATA_ROOT
WIKI = WIKI_DIR
RAW = EPISODES_DIR
OUT = WIKI / "stats"
OUT.mkdir(parents=True, exist_ok=True)

# Visual style: clean, minimal, dark-on-light. Matches Obsidian default.
plt.rcParams.update({
    "figure.dpi": 100,
    "font.family": "sans-serif",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.alpha": 0.25,
    "grid.linewidth": 0.5,
    "axes.titlesize": 14,
    "axes.titleweight": "bold",
    "axes.titlepad": 12,
    "axes.labelsize": 10,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
})

ACCENT = "#1a73e8"
ACCENT_DIM = "#9aa0a6"
HEAT_CMAP = "YlGnBu"


from _lib import is_music_channel  # type: ignore[attr-defined]


def load_episodes() -> pd.DataFrame:
    rows = []
    for m in RAW.glob("*/*/meta.json"):
        try:
            d = json.loads(m.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        show = m.parent.parent.name
        if is_music_channel(show):
            continue
        # Watched/listened day
        ds = (d.get("watched_at") or d.get("pub_date") or "")[:10]
        try:
            day = datetime.strptime(ds, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            day = None
        rows.append({
            "show": show,
            "is_yt": show.startswith("yt-"),
            "day": day,
            "year": day.year if day else None,
            "month": day.replace(day=1) if day else None,
            "listened": bool(d.get("listened") or (d.get("played_up_to") or 0) > 0),
            "duration_sec": d.get("duration_sec") or 0,
            "transcript_source": d.get("transcript_source"),
            "raw_dir": f"{show}/{m.parent.name}",
        })
    if not rows:
        return pd.DataFrame(columns=["show", "is_yt", "day", "year", "month", "listened", "duration_sec", "transcript_source", "raw_dir"])
    return pd.DataFrame(rows)


def load_wiki_meta() -> dict:
    eps = list((WIKI / "episodes").glob("*.md"))
    people = sorted([p.stem for p in (WIKI / "people").glob("*.md")])
    topics = sorted([p.stem for p in (WIKI / "topics").glob("*.md")])
    shows = sorted([p.stem for p in (WIKI / "shows").glob("*.md")])

    # Topic citations: parse each topic page for [[episodes/<slug>]] back-refs
    # to date-bucket their use. Episode date is parsed from the slug.
    topic_dates: dict[str, list[date]] = defaultdict(list)
    person_dates: dict[str, list[date]] = defaultdict(list)
    from podmind.frontmatter import read_date
    slug_to_date: dict[str, date] = {}
    for ep in eps:
        ds = read_date(ep)
        if not ds:
            continue
        try:
            slug_to_date[ep.stem] = datetime.strptime(ds[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    for tp in (WIKI / "topics").glob("*.md"):
        text = tp.read_text(errors="ignore")
        for m in re.finditer(r"\[\[episodes/([^\]]+)\]\]", text):
            d = slug_to_date.get(m.group(1))
            if d:
                topic_dates[tp.stem].append(d)
    for pp in (WIKI / "people").glob("*.md"):
        text = pp.read_text(errors="ignore")
        for m in re.finditer(r"\[\[episodes/([^\]]+)\]\]", text):
            d = slug_to_date.get(m.group(1))
            if d:
                person_dates[pp.stem].append(d)
    return {
        "wiki_eps": len(eps),
        "people": people,
        "topics": topics,
        "shows": shows,
        "topic_dates": topic_dates,
        "person_dates": person_dates,
    }


def chart_year_volume(df: pd.DataFrame) -> str:
    yearly = df[df.listened & df.year.notna()].groupby("year").size()
    yearly = yearly.reindex(range(int(yearly.index.min()), int(yearly.index.max()) + 1), fill_value=0)
    fig, ax = plt.subplots(figsize=(10, 4))
    bars = ax.bar(yearly.index.astype(int), yearly.values, color=ACCENT, edgecolor="white", linewidth=1)
    # Highlight current year
    cur = datetime.now().year
    for b, y in zip(bars, yearly.index):
        if int(y) == cur:
            b.set_color("#fb8c00")
    ax.set_title("Episodes listened per year")
    ax.set_ylabel("episodes")
    ax.set_xlabel("")
    ax.set_xticks(yearly.index.astype(int))
    for b, v in zip(bars, yearly.values):
        if v > 0:
            ax.text(b.get_x() + b.get_width() / 2, v + max(yearly.values) * 0.01, str(int(v)),
                    ha="center", va="bottom", fontsize=9)
    out = OUT / "year_volume.png"
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out.name


def chart_monthly_timeline(df: pd.DataFrame) -> str:
    df2 = df[df.listened & df.month.notna()]
    monthly = df2.groupby(["month", "is_yt"]).size().unstack(fill_value=0)
    monthly.columns = ["pc" if not c else "yt" for c in monthly.columns]
    monthly = monthly.reindex(columns=["pc", "yt"], fill_value=0)
    fig, ax = plt.subplots(figsize=(11, 4))
    ax.fill_between(monthly.index, 0, monthly["pc"], color=ACCENT, alpha=0.7, label="Pocket Casts")
    ax.fill_between(monthly.index, monthly["pc"], monthly["pc"] + monthly["yt"],
                    color="#fb8c00", alpha=0.6, label="YouTube")
    ax.set_title("Monthly listening volume — Pocket Casts vs YouTube")
    ax.set_ylabel("episodes / month")
    ax.legend(loc="upper left", frameon=False)
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    out = OUT / "monthly_timeline.png"
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out.name


def chart_calendar_heatmap(df: pd.DataFrame, months: int = 24) -> str:
    df2 = df[df.listened & df.day.notna()]
    today = date.today()
    start = date(today.year - (months // 12), today.month, 1)
    daily = df2[df2.day >= start].groupby("day").size()
    daily = daily.reindex(pd.date_range(start, today, freq="D").date, fill_value=0)

    # Reshape to (weeks, 7-day-of-week) for matrix render
    days = list(daily.index)
    counts = list(daily.values)
    first = days[0]
    pad = first.weekday()  # 0=Mon
    weeks = []
    week = [0] * pad
    for d, c in zip(days, counts):
        week.append(int(c))
        if len(week) == 7:
            weeks.append(week)
            week = []
    if week:
        week += [0] * (7 - len(week))
        weeks.append(week)
    mat = np.array(weeks).T  # (7, n_weeks)

    fig, ax = plt.subplots(figsize=(14, 3.5))
    vmax = max(int(mat.max()), 1)
    im = ax.imshow(mat, aspect="auto", cmap=HEAT_CMAP, vmin=0, vmax=vmax)
    ax.set_yticks(range(7))
    ax.set_yticklabels(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
    # x-axis: place ticks at first Monday of each month
    month_starts = []
    for i, week in enumerate(weeks):
        # week starts on date = start + i*7 - pad
        wstart = first + pd.Timedelta(days=i * 7 - pad) if i > 0 else first
        if wstart.day <= 7:
            month_starts.append((i, wstart.strftime("%b\n%Y")))
    ax.set_xticks([i for i, _ in month_starts])
    ax.set_xticklabels([s for _, s in month_starts])
    ax.set_title(f"Daily listening — last {months} months (lighter = quieter, darker = busier)")
    ax.set_xlabel("")
    cbar = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.02)
    cbar.set_label("episodes/day")
    fig.tight_layout()
    out = OUT / "calendar_heatmap.png"
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out.name


def chart_top_shows_split(df: pd.DataFrame, top_n: int = 20) -> str:
    df2 = df[df.listened]
    counts = df2.groupby(["show", "is_yt"]).size().reset_index(name="n")
    counts = counts.sort_values("n", ascending=False).head(top_n)
    counts = counts.iloc[::-1]
    fig, ax = plt.subplots(figsize=(10, max(5, top_n * 0.3)))
    colors = ["#fb8c00" if y else ACCENT for y in counts["is_yt"]]
    ax.barh(counts["show"], counts["n"], color=colors)
    ax.set_title(f"Top {top_n} shows (all-time)")
    ax.set_xlabel("episodes")
    for show, n, yt in zip(counts["show"], counts["n"], counts["is_yt"]):
        ax.text(n + 1, show, f"  {n}", va="center", fontsize=9)
    # Legend
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color=ACCENT, label="Pocket Casts"),
                       Patch(color="#fb8c00", label="YouTube")],
              loc="lower right", frameon=False)
    fig.tight_layout()
    out = OUT / "top_shows.png"
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out.name


def chart_topic_evolution(topic_dates: dict, top_n: int = 12) -> str | None:
    # Top N topics by total citations
    totals = Counter({t: len(d) for t, d in topic_dates.items()})
    top = [t for t, _ in totals.most_common(top_n)]
    if not top:
        return None
    # Year buckets
    rows = []
    for t in top:
        for d in topic_dates[t]:
            rows.append({"topic": t, "year": d.year})
    if not rows:
        return None
    df = pd.DataFrame(rows)
    pivot = df.groupby(["year", "topic"]).size().unstack(fill_value=0)
    pivot = pivot.reindex(columns=top, fill_value=0)

    fig, ax = plt.subplots(figsize=(11, 5))
    pivot.plot(kind="bar", stacked=True, ax=ax, colormap="tab20",
               edgecolor="white", linewidth=0.5, width=0.85)
    ax.set_title(f"Top {top_n} topics — citations per year")
    ax.set_ylabel("episode citations")
    ax.set_xlabel("")
    ax.legend(loc="center left", bbox_to_anchor=(1.0, 0.5), frameon=False, fontsize=9)
    plt.xticks(rotation=0)
    fig.tight_layout()
    out = OUT / "topic_evolution.png"
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out.name


def chart_coverage_funnel(df: pd.DataFrame, wiki_eps: int) -> str:
    listened = int(df.listened.sum())
    transcribed = int(((df.transcript_source.notna()) & (df.transcript_source != "none") & df.listened).sum())
    quarantined = int((df.transcript_source == "none").sum())
    untranscribed = int((df.transcript_source.isna() & df.listened).sum())

    labels = ["Listened/watched", "Transcribed", "In wiki"]
    vals = [listened, transcribed, wiki_eps]
    fig, ax = plt.subplots(figsize=(9, 3.5))
    ax.barh(labels[::-1], vals[::-1], color=[ACCENT, "#7b8db8", ACCENT_DIM][::-1])
    for i, v in enumerate(vals[::-1]):
        ax.text(v + max(vals) * 0.01, i, f"  {v:,}", va="center", fontsize=10)
    ax.set_title(f"Wiki coverage funnel  (quarantined: {quarantined}, untranscribed: {untranscribed})")
    ax.set_xlim(0, max(vals) * 1.15)
    fig.tight_layout()
    out = OUT / "coverage_funnel.png"
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out.name


def top_table(items: dict, n: int, header: str) -> str:
    rows = sorted(items.items(), key=lambda x: -len(x[1]))[:n]
    out = [f"| Rank | {header} | Episodes |", "|---:|---|---:|"]
    for i, (k, v) in enumerate(rows, 1):
        # Clean stem to readable label: strip yt-, replace dashes
        label = k.replace("-", " ").title()
        out.append(f"| {i} | [[{header.lower()}/{k}]] ({label}) | {len(v)} |")
    return "\n".join(out)


def build_md(df: pd.DataFrame, meta: dict, charts: dict) -> str:
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    listened = int(df.listened.sum())
    yt_count = int((df.listened & df.is_yt).sum())
    pc_count = listened - yt_count
    show_count = df[df.listened].show.nunique()
    listened_days = df[df.listened & df.day.notna()].day
    earliest = listened_days.min() if len(listened_days) else None
    latest = listened_days.max() if len(listened_days) else None
    span_years = (latest.year - earliest.year) if earliest and latest else 0
    duration_hr = int(df[df.listened]["duration_sec"].sum() / 3600)

    # Top tables
    top_topics = sorted(meta["topic_dates"].items(), key=lambda x: -len(x[1]))[:25]
    top_people = sorted(meta["person_dates"].items(), key=lambda x: -len(x[1]))[:25]

    # Rising / falling topics: compare last-12-months vs prior-12-months citation count
    today_d = date.today()
    last12_start = date(today_d.year - 1, today_d.month, 1)
    prior12_start = date(today_d.year - 2, today_d.month, 1)
    deltas = []
    for t, dates in meta["topic_dates"].items():
        last12 = sum(1 for d in dates if last12_start <= d <= today_d)
        prior12 = sum(1 for d in dates if prior12_start <= d < last12_start)
        if last12 + prior12 < 5:
            continue  # ignore low-volume noise
        deltas.append((t, last12, prior12, last12 - prior12))
    rising = sorted(deltas, key=lambda x: -x[3])[:10]
    falling = sorted(deltas, key=lambda x: x[3])[:10]

    md = []
    md.append("# Stats — Listening & Watching Habits")
    md.append("")
    md.append(f"_Generated {today}. Re-run via `./bin/build_stats.py`._")
    md.append("")
    md.append("## At a glance")
    md.append("")
    md.append(f"- **{listened:,}** episodes listened or watched since **{earliest}** — a {span_years}-year span")
    md.append(f"- **{pc_count:,}** Pocket Casts episodes / **{yt_count:,}** YouTube videos")
    md.append(f"- **{show_count:,}** distinct shows / channels")
    md.append(f"- **{duration_hr:,}** hours of tracked listening time (PC episodes; YouTube duration not always captured)")
    md.append(f"- **{meta['wiki_eps']:,}** episodes ingested into this wiki — across **{len(meta['shows'])}** show pages, **{len(meta['people'])}** people, **{len(meta['topics'])}** topics")
    md.append("")
    md.append("## Volume over time")
    md.append("")
    md.append(f"![year volume](stats/{charts['year_volume']})")
    md.append("")
    md.append("Steep ramp from 2022 onward — the LLM-podcast-wiki era. 2024 and 2025 both crossed 6,000 episodes; 2026 is on a similar pace YTD.")
    md.append("")
    md.append(f"![monthly timeline](stats/{charts['monthly_timeline']})")
    md.append("")
    md.append("YouTube watch-history (orange) is concentrated in 2023–2026 after Google Takeout imports surfaced the back catalogue. Pocket Casts (blue) is the steadier baseline.")
    md.append("")
    md.append("## Daily heatmap")
    md.append("")
    md.append(f"![calendar heatmap](stats/{charts['calendar_heatmap']})")
    md.append("")
    md.append("GitHub-contribution-style daily activity for the last 24 months. Darker squares = busier listening days. Reading the heatmap surfaces both routine and bursty consumption patterns.")
    md.append("")
    md.append("## Top shows")
    md.append("")
    md.append(f"![top shows](stats/{charts['top_shows']})")
    md.append("")
    md.append("All-time top 20. Pocket Casts shows (blue) and YouTube channels (orange) compete for the same attention budget; channel pages and show pages are interchangeable from the wiki's view.")
    md.append("")
    md.append("## Topic evolution")
    md.append("")
    if charts.get("topic_evolution"):
        md.append(f"![topic evolution](stats/{charts['topic_evolution']})")
        md.append("")
        md.append("Top 12 topics by total citation count, broken out by year. Stacked bars show the relative weight each topic has carried in your wiki year-over-year.")
        md.append("")

    md.append("### Rising in the last 12 months")
    md.append("")
    md.append("| Topic | last 12mo | prior 12mo | Δ |")
    md.append("|---|---:|---:|---:|")
    for t, l12, p12, delta in rising:
        md.append(f"| [[topics/{t}]] | {l12} | {p12} | **+{delta}** |")
    md.append("")
    md.append("### Cooling off")
    md.append("")
    md.append("| Topic | last 12mo | prior 12mo | Δ |")
    md.append("|---|---:|---:|---:|")
    for t, l12, p12, delta in falling:
        md.append(f"| [[topics/{t}]] | {l12} | {p12} | {delta} |")
    md.append("")

    md.append("## Top topics (all-time)")
    md.append("")
    md.append("| Rank | Topic | Citations |")
    md.append("|---:|---|---:|")
    for i, (t, dates) in enumerate(top_topics, 1):
        md.append(f"| {i} | [[topics/{t}]] | {len(dates)} |")
    md.append("")

    md.append("## Top people (all-time)")
    md.append("")
    md.append("| Rank | Person | Citations |")
    md.append("|---:|---|---:|")
    for i, (p, dates) in enumerate(top_people, 1):
        md.append(f"| {i} | [[people/{p}]] | {len(dates)} |")
    md.append("")

    md.append("## Coverage funnel")
    md.append("")
    md.append(f"![coverage funnel](stats/{charts['coverage_funnel']})")
    md.append("")
    md.append("How much of what you've actually heard makes it into the wiki. The gap between *listened* and *transcribed* is the daily-cron + whisper backlog; the gap between *transcribed* and *in wiki* is the LLM ingest queue.")
    md.append("")

    md.append("---")
    md.append("")
    md.append("**Methodology**: data sourced from `raw/episodes/*/*/meta.json`. Listened = `meta.listened == true OR played_up_to > 0`. Topic/people citations parsed from `[[episodes/<slug>]]` back-references in topic and person pages. Charts built with matplotlib; tables computed inline. See [bin/build_stats.py](../bin/build_stats.py) for the full pipeline.")
    md.append("")
    md.append("**Visualization choices** draw on patterns from Spotify Wrapped, Last.fm year-end reviews, Strava activity heatmaps, and GitHub contribution graphs:")
    md.append("- Calendar heatmap for daily granularity (intuitive at-a-glance density)")
    md.append("- Stacked bars for compositional change over time")
    md.append("- Top-N tables paired with rising/falling deltas (more actionable than raw rankings)")
    md.append("- Funnel chart for coverage (makes the gap visible)")
    md.append("")

    return "\n".join(md)


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    print("[1/3] loading episode data...")
    df = load_episodes()
    meta = load_wiki_meta()
    print(f"      {len(df):,} meta records, {int(df.listened.sum()):,} listened")

    print("[2/3] generating charts...")
    charts = {
        "year_volume": chart_year_volume(df),
        "monthly_timeline": chart_monthly_timeline(df),
        "calendar_heatmap": chart_calendar_heatmap(df, months=24),
        "top_shows": chart_top_shows_split(df, top_n=20),
        "topic_evolution": chart_topic_evolution(meta["topic_dates"], top_n=12),
        "coverage_funnel": chart_coverage_funnel(df, meta["wiki_eps"]),
    }
    for k, v in charts.items():
        if v:
            print(f"      ✓ {k}.png")

    print("[3/3] writing wiki/stats.md...")
    md = build_md(df, meta, charts)
    (WIKI / "stats.md").write_text(md)

    # Link from index.md
    idx = WIKI / "index.md"
    text = idx.read_text()
    if "[[stats]]" not in text:
        # Insert after the intro paragraph
        text = text.replace(
            "## Shows",
            "## At a glance\n\n- See [[stats]] for listening/watching activity and topic-evolution analytics.\n\n## Shows",
            1,
        )
        idx.write_text(text)
        print("      ✓ linked from index.md")

    print(f"\n✓ wiki/stats.md regenerated ({(WIKI / 'stats.md').stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
