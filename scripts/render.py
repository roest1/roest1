#!/usr/bin/env python3
"""Render data/languages.json into an animated SVG card.

Bars animate with SMIL (<animate> on width) and rows fade in with CSS
keyframes. Both survive GitHub's image proxy; JavaScript would not, so
there is none here.

Theme: two files, one per theme, each with a locked palette and no media
query. The card used to be a single SVG that flipped under
prefers-color-scheme: light, but a media query inside an SVG loaded through
<img> -- which is how GitHub serves README images -- never matches in WebKit
(bug 199134). The GitHub mobile app and Safari resolved it to light and drew
a white card while Chrome and Firefox drew a dark one.

So theme selection moves out of the SVG and into the README, which picks a
file via GitHub's #gh-dark-mode-only / #gh-light-mode-only fragments. Those
are GitHub-only and older than the <picture> element, but <picture> is still
unsupported in the mobile apps -- it renders no image at all there -- which
is the one client this whole change exists to fix.

One behaviour change worth knowing: the fragments follow the viewer's GitHub
theme setting, where the old media query followed their OS. The card now
matches the page around it rather than the system.
"""

import json
import os

TOP_N = 8
WIDTH = 540
PAD = 24
ROW_H = 30
BAR_X = 136
BAR_W = 260
BAR_H = 8
COUNT_X = WIDTH - PAD  # right edge of the "N repos" column
MIN_BAR = 6  # so a one-repo language is still visibly a bar

# Same six roles per theme, so the rules below never need to know which one
# they got. Kept as data rather than two CSS blobs -- a palette tweak is then
# a one-line edit that cannot leave the themes structurally out of sync.
THEMES = {
    "dark": {
        "bg": "#0d1117", "edge": "#30363d", "title": "#e6edf3",
        "text": "#c9d1d9", "dim": "#8b949e", "track": "#21262d",
    },
    "light": {
        "bg": "#ffffff", "edge": "#d0d7de", "title": "#1f2328",
        "text": "#1f2328", "dim": "#59636e", "track": "#eaeef2",
    },
}

CSS_BODY = """
  .card  { fill: var(--bg); stroke: var(--edge); }
  .title { font: 600 17px "Segoe UI", Ubuntu, "Helvetica Neue", sans-serif; fill: var(--title); }
  .sub   { font: 400 11.5px "Segoe UI", Ubuntu, "Helvetica Neue", sans-serif; fill: var(--dim); }
  .name  { font: 500 13px "Segoe UI", Ubuntu, "Helvetica Neue", sans-serif; fill: var(--text); }
  .meta  { font: 400 12px "Segoe UI", Ubuntu, "Helvetica Neue", sans-serif; fill: var(--dim); }
  .track { fill: var(--track); }
  /* No opacity:0 in the base rules, and animation-fill-mode: both rather
     than forwards. `both` still hides each row through its delay, but a
     renderer that ignores CSS animation falls back to the finished card
     instead of a blank one. Same reasoning puts the final width on the bar
     rects below, with SMIL animating up from zero. */
  .fade  { animation: fade .5s ease-out both; }
  .row   { animation: rise .5s cubic-bezier(.2,.7,.2,1) both; }
  @keyframes fade { from { opacity: 0; } to { opacity: 1; } }
  @keyframes rise { from { opacity: 0; transform: translateY(7px); }
                    to   { opacity: 1; transform: translateY(0); } }
"""


def css(theme):
    """Custom properties for one theme, prepended to the shared rules."""
    palette = " ".join(f"--{role}:{hex_};" for role, hex_ in THEMES[theme].items())
    return f"\n  svg {{ {palette} }}{CSS_BODY}"


def esc(text):
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def render(data, theme):
    langs = data["languages"][:TOP_N]
    top = max((lang["repos"] for lang in langs), default=1)
    # 68 above the first row, then the rows, then a bottom margin that matches
    # the side padding. The old footer line used to occupy that band.
    height = 68 + ROW_H * len(langs) + 12

    subtitle = f"ranked by projects, not bytes · {data['repos_counted']} repos"

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" '
        f'aria-label="Most used languages, ranked by number of projects">',
        f"<style>{css(theme)}</style>",
        f'<rect class="card" x="0.5" y="0.5" width="{WIDTH - 1}" height="{height - 1}" '
        f'rx="6" stroke-width="1"/>',
        f'<text class="title fade" x="{PAD}" y="34">Most Used Languages</text>',
        f'<text class="sub fade" x="{PAD}" y="54" style="animation-delay:.1s">'
        f"{esc(subtitle)}</text>",
    ]

    for i, lang in enumerate(langs):
        cy = 83 + i * ROW_H
        bar_y = cy - BAR_H // 2 - 1
        width = max(MIN_BAR, BAR_W * lang["repos"] / top)
        delay = 0.18 + i * 0.09
        plural = "repo" if lang["repos"] == 1 else "repos"

        out += [
            f'<g class="row" style="animation-delay:{delay:.2f}s">',
            f'  <text class="name" x="{PAD}" y="{cy + 4}">{esc(lang["name"])}</text>',
            f'  <rect class="track" x="{BAR_X}" y="{bar_y}" width="{BAR_W}" '
            f'height="{BAR_H}" rx="{BAR_H / 2}"/>',
            f'  <rect x="{BAR_X}" y="{bar_y}" width="{width:.1f}" height="{BAR_H}" '
            f'rx="{BAR_H / 2}" fill="{esc(lang["color"])}">',
            f'    <animate attributeName="width" from="0" to="{width:.1f}" '
            f'begin="{delay + 0.1:.2f}s" dur="0.75s" fill="freeze" '
            f'calcMode="spline" keyTimes="0;1" keySplines="0.2 0.8 0.2 1"/>',
            "  </rect>",
            f'  <text class="meta" x="{COUNT_X}" y="{cy + 4}" text-anchor="end">'
            f'{lang["repos"]} {plural}</text>',
            "</g>",
        ]

    out += ["</svg>"]
    return "\n".join(out)


def main():
    with open("data/languages.json") as f:
        data = json.load(f)

    os.makedirs("cards", exist_ok=True)
    # Both files every run. Rendering only the changed one would let the two
    # drift apart silently, and each is ~6 KB.
    for theme in THEMES:
        path = f"cards/top-langs-{theme}.svg"
        with open(path, "w") as f:
            f.write(render(data, theme))
        print(f"wrote {path} ({len(data['languages'][:TOP_N])} languages)")


if __name__ == "__main__":
    main()
