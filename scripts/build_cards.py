#!/usr/bin/env python3
"""Render one SVG card per favourite repo for the profile README.

Each card shows the logo from the top of the repo's own README on the left,
and the repo name, description, primary language and star count on the right.
Cards come in a light and a dark variant so the README can pick one with a
<picture> element.

Needs only the standard library plus a GitHub token in GITHUB_TOKEN (optional
for public repos, but the unauthenticated rate limit is low). Pillow is used to
shrink raster logos when it is installed; without it the original bytes are
embedded as-is.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass
from html import escape
from pathlib import Path

OWNER = "dimitritholen"
REPOS = ["tasqx", "1337-claude", "proudhuman"]
OUT_DIR = Path(__file__).resolve().parent.parent / "cards"

CARD_W, CARD_H = 720, 148
LOGO_W = 132
LOGO_PAD = 16
TEXT_X = LOGO_W + 18
DESC_WIDTH = 62  # characters per description line
DESC_LINES = 2

# GitHub's own language colours for the languages these repos use, plus a few
# likely neighbours. Unknown languages fall back to a neutral grey.
LANGUAGE_COLOURS = {
    "Rust": "#dea584", "Shell": "#89e051", "HTML": "#e34c26", "Python": "#3572A5",
    "TypeScript": "#3178c6", "JavaScript": "#f1e05a", "Go": "#00ADD8", "Ruby": "#701516",
    "C": "#555555", "C++": "#f34b7d", "C#": "#178600", "Java": "#b07219",
    "Kotlin": "#A97BFF", "Swift": "#F05138", "Lua": "#000080", "CSS": "#663399",
    "Vue": "#41b883", "PHP": "#4F5D95", "Dockerfile": "#384d54",
}

THEMES = {
    "light": {
        "canvas": "#ffffff", "well": "#f6f8fa", "border": "#d0d7de", "border_soft": "#d8dee4",
        "fg": "#1f2328", "muted": "#656d76", "link": "#0969da",
        "pill_bg": "#ddf4ff", "pill_fg": "#0969da",
    },
    "dark": {
        "canvas": "#0d1117", "well": "#161b22", "border": "#30363d", "border_soft": "#21262d",
        "fg": "#e6edf3", "muted": "#8b949e", "link": "#4493f8",
        "pill_bg": "#122d4a", "pill_fg": "#79c0ff",
    },
}

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"

# Octicons: repo and star.
REPO_ICON = ("M2 2.5A2.5 2.5 0 0 1 4.5 0h8.75a.75.75 0 0 1 .75.75v12.5a.75.75 0 0 1-.75.75h-2.5"
             "a.75.75 0 0 1 0-1.5h1.75v-2h-8a1 1 0 0 0-.714 1.7.75.75 0 1 1-1.072 1.05A2.495 2.495"
             " 0 0 1 2 11.5Zm10.5-1h-8a1 1 0 0 0-1 1v6.708A2.486 2.486 0 0 1 4.5 9h8ZM5 12.25a.25.25"
             " 0 0 1 .25-.25h3.5a.25.25 0 0 1 .25.25v3.25a.25.25 0 0 1-.4.2l-1.45-1.087a.249.249 0 0"
             " 0-.3 0L5.4 15.7a.25.25 0 0 1-.4-.2Z")
STAR_ICON = ("M8 .25a.75.75 0 0 1 .673.418l1.882 3.815 4.21.612a.75.75 0 0 1 .416 1.279l-3.046"
             " 2.97.719 4.192a.751.751 0 0 1-1.088.791L8 12.347l-3.766 1.98a.75.75 0 0 1-1.088-.79"
             "l.72-4.194L.818 6.374a.75.75 0 0 1 .416-1.28l4.21-.611L7.327.668A.75.75 0 0 1 8 .25Z")


@dataclass
class Logo:
    light: bytes
    light_mime: str
    dark: bytes | None = None
    dark_mime: str | None = None


@dataclass
class Repo:
    name: str
    description: str
    language: str | None
    stars: int
    license: str | None
    default_branch: str
    logo: Logo | None


def github(url: str, accept: str = "application/vnd.github+json") -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "profile-cards"})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def fetch_json(url: str) -> dict:
    return json.loads(github(url))


IMG_TAG = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)
SOURCE_TAG = re.compile(
    r"<source\b[^>]*?\bmedia\s*=\s*[\"']\(prefers-color-scheme:\s*dark\)[\"'][^>]*?\bsrcset\s*=\s*[\"']([^\"']+)[\"']"
    r"|<source\b[^>]*?\bsrcset\s*=\s*[\"']([^\"']+)[\"'][^>]*?\bmedia\s*=\s*[\"']\(prefers-color-scheme:\s*dark\)[\"']",
    re.I,
)
MD_IMG = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")


def is_badge(url: str) -> bool:
    return any(s in url for s in ("shields.io", "badge", "img.shields"))


def find_logo_urls(readme: str) -> tuple[str, str | None] | None:
    """Return (light_url, dark_url) for the first image in the README header.

    The header is everything before the first '## ' heading. Badges are skipped.
    """
    head = re.split(r"^##\s", readme, maxsplit=1, flags=re.M)[0]
    head = re.sub(r"<!--.*?-->", "", head, flags=re.S)
    candidates: list[tuple[int, str]] = []
    for m in IMG_TAG.finditer(head):
        candidates.append((m.start(), m.group(1)))
    for m in MD_IMG.finditer(head):
        candidates.append((m.start(), m.group(1)))
    candidates = [c for c in sorted(candidates) if not is_badge(c[1])]
    if not candidates:
        return None
    light = candidates[0][1]
    dark = None
    m = SOURCE_TAG.search(head)
    if m:
        dark = m.group(1) or m.group(2)
    return light, dark


def resolve(url: str, repo: str, branch: str) -> str:
    if url.startswith(("http://", "https://")):
        return url
    return f"https://raw.githubusercontent.com/{OWNER}/{repo}/{branch}/{url.lstrip('./')}"


def mime_for(url: str, data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if b"<svg" in data[:2000]:
        return "image/svg+xml"
    if url.lower().endswith(".webp"):
        return "image/webp"
    return "application/octet-stream"


def shrink(data: bytes, mime: str, max_px: int = 480) -> tuple[bytes, str]:
    """Downscale a raster logo so the card stays small. No-op for SVG or without Pillow."""
    if mime == "image/svg+xml":
        return data, mime
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return data, mime
    im = Image.open(io.BytesIO(data))
    if max(im.size) <= max_px:
        return data, mime
    im.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), "image/png"


def fetch_logo(repo: str, branch: str, readme: str) -> Logo | None:
    found = find_logo_urls(readme)
    if not found:
        return None
    light_url, dark_url = found
    light = github(resolve(light_url, repo, branch), accept="*/*")
    light, light_mime = shrink(light, mime_for(light_url, light))
    logo = Logo(light=light, light_mime=light_mime)
    if dark_url:
        dark = github(resolve(dark_url, repo, branch), accept="*/*")
        logo.dark, logo.dark_mime = shrink(dark, mime_for(dark_url, dark))
    return logo


def fetch_repo(name: str) -> Repo:
    meta = fetch_json(f"https://api.github.com/repos/{OWNER}/{name}")
    readme = github(f"https://api.github.com/repos/{OWNER}/{name}/readme",
                    accept="application/vnd.github.raw+json").decode("utf-8", "replace")
    lic = meta.get("license") or {}
    return Repo(
        name=meta["name"],
        description=meta.get("description") or "",
        language=meta.get("language"),
        stars=int(meta.get("stargazers_count") or 0),
        license=lic.get("spdx_id") if lic.get("spdx_id") not in (None, "NOASSERTION") else None,
        default_branch=meta.get("default_branch") or "main",
        logo=fetch_logo(name, meta.get("default_branch") or "main", readme),
    )


def data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def wrap_description(text: str) -> list[str]:
    lines = textwrap.wrap(text, width=DESC_WIDTH)
    if len(lines) > DESC_LINES:
        lines = lines[:DESC_LINES]
        last = lines[-1][: DESC_WIDTH - 1]
        cut = last.rfind(" ")
        if cut > DESC_WIDTH // 2:
            last = last[:cut]
        lines[-1] = last.rstrip(" ,;:.") + "…"
    return lines


def render_card(repo: Repo, theme: str) -> str:
    t = THEMES[theme]
    logo_svg = ""
    if repo.logo:
        data, mime = repo.logo.light, repo.logo.light_mime
        if theme == "dark" and repo.logo.dark:
            data, mime = repo.logo.dark, repo.logo.dark_mime or repo.logo.light_mime
        inner = LOGO_W - 2 * LOGO_PAD
        logo_svg = (
            f'<image x="{LOGO_PAD}" y="{LOGO_PAD}" width="{inner}" height="{CARD_H - 2 * LOGO_PAD}" '
            f'preserveAspectRatio="xMidYMid meet" href="{data_uri(data, mime)}"/>'
        )
    else:
        # No logo in the README: show the repo octicon large, in the muted colour.
        logo_svg = (f'<g transform="translate({LOGO_W / 2 - 24},{CARD_H / 2 - 24}) scale(3)" fill="{t["muted"]}">'
                    f'<path d="{REPO_ICON}"/></g>')

    desc_lines = wrap_description(repo.description)
    desc_svg = "".join(
        f'<text x="{TEXT_X}" y="{62 + i * 19}" font-size="13" fill="{t["muted"]}">{escape(line)}</text>'
        for i, line in enumerate(desc_lines)
    )

    foot_y = CARD_H - 22
    foot: list[str] = []
    x = TEXT_X
    if repo.language:
        colour = LANGUAGE_COLOURS.get(repo.language, "#8b949e")
        foot.append(f'<circle cx="{x + 5}" cy="{foot_y - 4}" r="5" fill="{colour}"/>')
        foot.append(f'<text x="{x + 16}" y="{foot_y}" font-size="12" fill="{t["muted"]}">{escape(repo.language)}</text>')
        x += 16 + 7 * len(repo.language) + 18
    foot.append(f'<g transform="translate({x},{foot_y - 12}) scale(0.875)" fill="{t["muted"]}"><path d="{STAR_ICON}"/></g>')
    foot.append(f'<text x="{x + 19}" y="{foot_y}" font-size="12" fill="{t["muted"]}">{repo.stars}</text>')
    x += 19 + 7 * len(str(repo.stars)) + 18
    if repo.license:
        foot.append(f'<text x="{x}" y="{foot_y}" font-size="12" fill="{t["muted"]}">{escape(repo.license)}</text>')

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{CARD_W}" height="{CARD_H}" viewBox="0 0 {CARD_W} {CARD_H}" role="img" aria-labelledby="t">
<title id="t">{escape(repo.name)}: {escape(repo.description)}</title>
<style>text{{font-family:{FONT};}}</style>
<rect x="0.5" y="0.5" width="{CARD_W - 1}" height="{CARD_H - 1}" rx="8" fill="{t["canvas"]}" stroke="{t["border"]}"/>
<clipPath id="well"><rect x="0.5" y="0.5" width="{LOGO_W}" height="{CARD_H - 1}" rx="8"/></clipPath>
<g clip-path="url(#well)"><rect x="0.5" y="0.5" width="{LOGO_W + 8}" height="{CARD_H - 1}" rx="8" fill="{t["well"]}"/></g>
<line x1="{LOGO_W + 0.5}" y1="1" x2="{LOGO_W + 0.5}" y2="{CARD_H - 1}" stroke="{t["border_soft"]}"/>
{logo_svg}
<g transform="translate({TEXT_X},{22}) scale(1)" fill="{t["muted"]}"><path d="{REPO_ICON}"/></g>
<text x="{TEXT_X + 22}" y="{35}" font-size="16" font-weight="600" fill="{t["link"]}">{escape(repo.name)}</text>
{desc_svg}
{"".join(foot)}
</svg>
"""


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name in REPOS:
        try:
            repo = fetch_repo(name)
        except urllib.error.HTTPError as e:
            print(f"{name}: GitHub API error {e.code}", file=sys.stderr)
            return 1
        for theme in THEMES:
            suffix = "" if theme == "light" else "-dark"
            path = OUT_DIR / f"{name}{suffix}.svg"
            path.write_text(render_card(repo, theme), encoding="utf-8")
            print(f"wrote {path.relative_to(OUT_DIR.parent)} ({path.stat().st_size // 1024} KB)")
        print(f"  {name}: logo={'yes' if repo.logo else 'no'}"
              f"{' (+dark)' if repo.logo and repo.logo.dark else ''}, "
              f"{repo.language}, {repo.stars} stars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
