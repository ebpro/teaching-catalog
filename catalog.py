#!/usr/bin/env python3
"""teaching-catalog generator.

Scans every ``notebook-*`` / ``lecture-*`` repository in the ``ebpro`` GitHub
org, builds a per-repo git-topology view (last stable tag, current trunk head,
and every other branch ranked by divergence) and renders a self-contained
static ``index.html`` plus a machine-readable ``manifest.json`` under
``./public/``.

Pure Python 3 stdlib. No third-party dependencies, no network calls other than
the GitHub REST API, and no threads (all calls are strictly sequential so the
rate limit is respected).

Auth: reads the token from the ``GITHUB_TOKEN`` environment variable.
"""

import html
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# --- configuration --------------------------------------------------------

ORG = "ebpro"
# Edit this to change which repos appear in the catalog (name-prefix match).
PREFIXES = ("notebook-", "lecture-")
API = "https://api.github.com"
OUT_DIR = "public"
USER_AGENT = "ebpro-teaching-catalog"
GITHUB_IO_BASE = "https://ebpro.github.io"

# --- API layer ------------------------------------------------------------

_call_count = 0
TOKEN = ""


def _request(url, none_on=()):
    """GET ``url`` and return the parsed JSON body.

    On HTTP 403 or 429, sleep 60s and retry exactly once, then fail loudly
    (RuntimeError -> non-zero exit). Any other HTTP error fails immediately.

    If an HTTP status code is in ``none_on``, that response is treated as a
    benign "absent" state and None is returned instead of raising. Used for:
      - 409 on the commits endpoint ("Git Repository is empty")
      - 404 on the compare endpoint (no common ancestor / branch gone)
    """
    global _call_count
    _call_count += 1
    last_err = None
    for attempt in (1, 2):
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + TOKEN,
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (403, 429) and attempt == 1:
                sys.stderr.write(
                    "WARN: HTTP %d on %s - sleeping 60s, retrying once\n"
                    % (e.code, url))
                time.sleep(60)
                continue
            if e.code in none_on:
                return None
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise RuntimeError(
                "HTTP %d for %s: %s" % (e.code, url, body[:500]))
        except Exception as e:
            raise RuntimeError("Request failed for %s: %s" % (url, e))
    raise RuntimeError("Gave up on %s after one retry: %s" % (url, last_err))


def list_repos():
    """Return every org repo, paginating through all pages."""
    repos = []
    page = 1
    while True:
        url = "%s/orgs/%s/repos?per_page=100&type=all&page=%d" % (API, ORG, page)
        data = _request(url)
        if not isinstance(data, list) or not data:
            break
        repos.extend(data)
        if len(data) < 100:
            break
        page += 1
    return repos


# --- per-repo topology ----------------------------------------------------

def _short(sha):
    return (sha or "")[:7]


def _first_line(msg):
    if not msg:
        return ""
    lines = [ln for ln in msg.strip().splitlines() if ln.strip()]
    return lines[0].strip() if lines else ""


def _date10(date):
    return (date or "")[:10]


def fetch_stable(repo):
    """Most-recent tag by committer date, or None if the repo has no tags."""
    name = repo["name"]
    tags = _request("%s/repos/%s/%s/tags?per_page=30" % (API, ORG, name))
    if not tags:
        return None
    best = None
    for t in tags:
        sha = t["commit"]["sha"]
        c = _request("%s/repos/%s/%s/commits/%s" % (API, ORG, name, sha))
        date = c["commit"]["committer"]["date"]
        subject = _first_line(c["commit"]["message"])
        if best is None or date > best["date"]:
            best = {"tag": t["name"], "sha": sha, "date": date, "subject": subject}
    return best


def fetch_trunk(repo):
    """Head of the default branch (sha, date, subject).

    Returns None for repos with no commits yet (empty placeholder repos).
    """
    name = repo["name"]
    db = repo["default_branch"]
    c = _request("%s/repos/%s/%s/commits/%s" % (API, ORG, name, db),
                 none_on=(409,))
    if c is None:
        return None
    return {
        "branch": db,
        "sha": c["sha"],
        "date": c["commit"]["committer"]["date"],
        "subject": _first_line(c["commit"]["message"]),
    }


def fetch_branches(repo):
    """Every non-default branch with ahead/behind vs trunk.

    Sorted by ``ahead`` desc, then name asc; branches with no computable
    divergence (no common ancestor, e.g. generated ``gh-pages``) are sorted
    last with ``ahead``/``behind`` = None. No per-branch commit subjects are
    fetched (keeps the API call count bounded).
    """
    name = repo["name"]
    db = repo["default_branch"]
    branches = _request("%s/repos/%s/%s/branches?per_page=100" % (API, ORG, name))
    result = []
    for b in branches:
        bn = b["name"]
        if bn == db:
            continue
        comp = _request("%s/repos/%s/%s/compare/%s...%s" % (API, ORG, name, db, bn),
                        none_on=(404,))
        if comp is None:
            result.append({"name": bn, "sha": b["commit"]["sha"],
                           "ahead": None, "behind": None})
        else:
            result.append({"name": bn, "sha": b["commit"]["sha"],
                           "ahead": comp.get("ahead_by", 0),
                           "behind": comp.get("behind_by", 0)})
    result.sort(key=lambda x: (x["ahead"] is None,
                               -(x["ahead"] if x["ahead"] is not None else 0),
                               x["name"]))
    return result


# --- HTML rendering -------------------------------------------------------

CSS = """
:root { --bg:#f6f7f9; --card:#ffffff; --ink:#1f2328; --muted:#6a737d;
  --line:#e1e4e8; --accent:#0969da; --accent2:#bc4c00; }
* { box-sizing:border-box; }
body { margin:0; padding:24px; background:var(--bg); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  line-height:1.5; }
.wrap { max-width:900px; margin:0 auto; }
h1 { font-size:1.6rem; margin:0 0 4px; }
.meta { color:var(--muted); font-size:.9rem; margin-bottom:8px; }
.meta a { color:var(--accent); text-decoration:none; }
.meta a:hover { text-decoration:underline; }
.group { font-size:.78rem; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); margin:28px 0 10px; border-bottom:1px solid var(--line);
  padding-bottom:5px; font-weight:600; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px;
  padding:14px 16px; margin-bottom:12px; }
.card h2 { font-size:1.05rem; margin:0 0 2px; }
.card h2 a { color:var(--ink); text-decoration:none; }
.card h2 a:hover { color:var(--accent); }
.live { font-size:.8rem; margin-bottom:6px; }
.live a { color:var(--accent); text-decoration:none; }
.live a:hover { text-decoration:underline; }
.row { margin:5px 0; font-size:.9rem; }
.label { display:inline-block; min-width:130px; color:var(--muted); font-weight:600; }
code { font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  font-size:.85em; background:var(--bg); border-radius:4px; padding:0 4px; }
.none { color:var(--muted); }
ul.features { list-style:none; margin:2px 0 0; padding:0; }
ul.features li { margin:3px 0; font-size:.85rem; }
.pill { display:inline-block; background:#fff1e5; color:var(--accent2);
  border:1px solid #ffd8b3; border-radius:10px; font-size:.68rem; padding:0 6px;
  margin-left:6px; vertical-align:middle; }
"""


def _render_card(name, r):
    esc = html.escape
    db = r["default_branch"]
    stable = r["stable"]
    trunk = r["trunk"]
    branches = r["branches"]

    if stable:
        stable_html = "<code>%s</code> &rarr; <code>%s</code> &middot; <code>%s</code> &middot; %s" % (
            esc(stable["tag"]), esc(_short(stable["sha"])),
            esc(_date10(stable["date"])), esc(stable["subject"]))
    else:
        stable_html = '<span class="none">&mdash; (no tags)</span>'

    if trunk is None:
        trunk_html = '<span class="none">&mdash; (empty repo, no commits)</span>'
    else:
        trunk_html = "<code>%s</code> &rarr; <code>%s</code> &middot; <code>%s</code> &middot; %s" % (
            esc(trunk["branch"]), esc(_short(trunk["sha"])),
            esc(_date10(trunk["date"])), esc(trunk["subject"]))

    if branches:
        items = []
        for b in branches:
            if b["ahead"] is None:
                div = " &middot; n/a (no common ancestor)"
                pill = ""
            else:
                div = " &middot; +%d/&minus;%d vs trunk" % (b["ahead"], b["behind"])
                pill = ' <span class="pill">divergent</span>' if b["ahead"] > 0 else ""
            items.append(
                "<li><code>%s</code> &rarr; <code>%s</code>%s%s</li>"
                % (esc(b["name"]), esc(_short(b["sha"])), div, pill))
        features_block = (
            '<div class="row"><span class="label">Feature heads:</span></div>'
            '<ul class="features">%s</ul>' % "".join(items))
    else:
        features_block = (
            '<div class="row"><span class="label">Feature heads:</span> '
            '<span class="none">&mdash; (none)</span></div>')

    parts = []
    parts.append('<section class="card">')
    parts.append('  <h2><a href="https://github.com/%s/%s">%s</a></h2>'
                 % (esc(ORG), esc(name), esc(name)))
    parts.append('  <div class="live"><a href="%s/%s/" target="_blank" rel="noopener">Live site &rarr;</a></div>'
                 % (esc(GITHUB_IO_BASE), esc(name)))
    parts.append('  <div class="row"><span class="label">Stable (last tag):</span> %s</div>'
                 % stable_html)
    parts.append('  <div class="row"><span class="label">Trunk (current):</span> %s</div>'
                 % trunk_html)
    parts.append("  " + features_block)
    parts.append("</section>")
    return "\n".join(parts)


def _render_html(manifest):
    esc = html.escape
    repos = manifest["repos"]
    generated = esc(manifest["generated_at"])
    count = manifest["count"]

    notebook = sorted(n for n in repos if n.startswith("notebook-"))
    lecture = sorted(n for n in repos if n.startswith("lecture-"))
    other = sorted(n for n in repos if n not in notebook and n not in lecture)

    groups = []
    if notebook:
        groups.append(("notebooks", notebook))
    if lecture:
        groups.append(("lectures", lecture))
    if other:
        groups.append(("other", other))

    body = []
    for title, names in groups:
        body.append('<h3 class="group">%s</h3>' % esc(title))
        for n in names:
            body.append(_render_card(n, repos[n]))

    parts = []
    parts.append("<!doctype html>")
    parts.append('<html lang="en">')
    parts.append("<head>")
    parts.append('<meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append("<title>ebpro teaching catalog</title>")
    parts.append("<style>%s</style>" % CSS)
    parts.append("</head>")
    parts.append("<body>")
    parts.append('<div class="wrap">')
    parts.append("<h1>ebpro teaching catalog</h1>")
    parts.append(
        '<div class="meta">Generated %s &middot; %d repos &middot; auto-updates every 6h '
        '&middot; <a href="https://github.com/ebpro/teaching-catalog">teaching-catalog</a></div>'
        % (generated, count))
    parts.extend(body)
    parts.append("</div>")
    parts.append("</body>")
    parts.append("</html>")
    parts.append("")
    return "\n".join(parts)


# --- entry point ----------------------------------------------------------

def main():
    global TOKEN
    TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
    if not TOKEN:
        sys.stderr.write("ERROR: GITHUB_TOKEN environment variable is empty\n")
        return 1

    repos = list_repos()
    kept = [r for r in repos
            if r["name"].startswith(PREFIXES) and not r.get("archived")]
    kept.sort(key=lambda r: r["name"])

    data = {}
    for r in kept:
        name = r["name"]
        sys.stderr.write("Processing %s ...\n" % name)
        data[name] = {
            "default_branch": r["default_branch"],
            "stable": fetch_stable(r),
            "trunk": fetch_trunk(r),
            "branches": fetch_branches(r),
        }

    manifest = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "org": ORG,
        "count": len(data),
        "repos": data,
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(_render_html(manifest))

    sys.stderr.write(
        "Wrote %s/index.html and %s/manifest.json (%d repos)\n"
        % (OUT_DIR, OUT_DIR, len(data)))
    print("Total GitHub API requests: %d" % _call_count)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        sys.stderr.write("FATAL: %s\n" % e)
        sys.exit(1)
