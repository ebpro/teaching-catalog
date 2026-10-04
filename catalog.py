#!/usr/bin/env python3
"""teaching-catalog generator.

Scans every ``notebook-*`` / ``lecture-*`` / ``sample-*`` / ``demo-*``
repository in the ``ebpro`` GitHub org, builds a per-repo git-topology view
(last stable tag, current trunk head, and every other branch ranked by
divergence) plus per-repo metadata (description, topics, README title) and
per-ref website links, and renders a self-contained static ``index.html`` plus
a machine-readable ``manifest.json`` under ``./public/``.

Pure Python 3 stdlib. No third-party dependencies. Network calls are the GitHub
REST API (always) and, when ``CF_API_TOKEN`` is set, the Cloudflare Pages API
(best-effort: a CF failure never breaks the catalog). No threads (all calls are
strictly sequential so the rate limit is respected).

Auth: reads ``GITHUB_TOKEN`` (required) and ``CF_API_TOKEN`` (optional) from the
environment.
"""

import base64
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# --- configuration --------------------------------------------------------

ORG = "ebpro"
# Edit this to change which repos appear in the catalog (name-prefix match).
PREFIXES = ("notebook-", "lecture-", "sample-", "demo-")
API = "https://api.github.com"
OUT_DIR = "public"
USER_AGENT = "ebpro-teaching-catalog"
GITHUB_IO_BASE = "https://ebpro.github.io"

# --- API layer ------------------------------------------------------------

_call_count = 0
TOKEN = ""
# Retry policy for the strict GitHub helper: on a transient failure (HTTP
# 403/429 rate limit, or a network-level blip such as a DNS/connection error)
# retry with exponential backoff, up to _MAX_ATTEMPTS total attempts, and only
# raise if it keeps failing. Best-effort callers (README) wrap this in
# try/except so they can never crash the run.
_MAX_ATTEMPTS = 4
_BACKOFFS = (30, 60, 120)  # seconds before retry 1, 2, 3
_RETRYABLE_HTTP = (403, 429)


def _request(url, none_on=(), attempts=_MAX_ATTEMPTS):
    """GET ``url`` and return the parsed JSON body.

    Retries with exponential backoff on transient failures (HTTP 403/429 rate
    limit, or a network-level blip such as a DNS/connection error), up to
    ``attempts`` total attempts, then raises RuntimeError (non-zero exit).
    Non-retryable HTTP errors fail immediately.

    If an HTTP status code is in ``none_on``, that response is treated as a
    benign "absent" state and None is returned instead of raising. Used for:
      - 409 on the commits endpoint ("Git Repository is empty")
      - 404 on the compare endpoint (no common ancestor / branch gone)
    """
    global _call_count
    last_err = None
    for attempt in range(1, attempts + 1):
        _call_count += 1
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
            if e.code in _RETRYABLE_HTTP and attempt < attempts:
                delay = _BACKOFFS[min(attempt - 1, len(_BACKOFFS) - 1)]
                sys.stderr.write(
                    "WARN: HTTP %d on %s - sleeping %ds (attempt %d/%d)\n"
                    % (e.code, url, delay, attempt, attempts))
                time.sleep(delay)
                last_err = e
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
        except (urllib.error.URLError, OSError) as e:
            # Transient network-level failure (DNS blip, connection reset,
            # timeout): back off and retry, like a rate limit.
            last_err = e
            if attempt < attempts:
                delay = _BACKOFFS[min(attempt - 1, len(_BACKOFFS) - 1)]
                sys.stderr.write(
                    "WARN: network error on %s (%s) - sleeping %ds "
                    "(attempt %d/%d)\n" % (url, e, delay, attempt, attempts))
                time.sleep(delay)
                continue
    raise RuntimeError("Gave up on %s after %d attempts: %s"
                       % (url, attempts, last_err))


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


# --- Cloudflare Pages API layer (best-effort; never raises) ---------------
#
# Used only when CF_API_TOKEN is set (i.e. in CI). Locally the token is empty
# and no CF calls are made (all non-main refs render with no link). Every CF
# failure is swallowed -> cf_previews = {} so a Cloudflare problem can never
# break the (GitHub-backed) catalog.

CF_TOKEN = ""
CF_API = "https://api.cloudflare.com/client/v4"
_cf_call_count = 0
_cf_account_id = None
_cf_account_fetched = False
_cf_accounts_status = None
# Per-project deployment-lookup outcome counters (for a self-diagnosing
# summary): a 200 means the project exists; 404 means no CF Pages project for
# that repo; 401/403 means the token lacks permission to read it (a scope
# issue that must be reported, not silently swallowed).
_cf_proj_found = 0
_cf_proj_404 = 0
_cf_proj_auth = 0
_cf_proj_other = 0


def _cf_get(url):
    """GET a Cloudflare API url. Returns a (body, status) tuple:
    (parsed_json, 200) on success; (None, http_code) on an HTTP error;
    (None, -1) on any other failure (network, timeout, ...). Never raises."""
    global _cf_call_count
    _cf_call_count += 1
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + CF_TOKEN,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp), resp.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception:
        return None, -1


def cf_account_id():
    """Fetch the Cloudflare account id ONCE (cached). None on any failure."""
    global _cf_account_id, _cf_account_fetched, _cf_accounts_status
    if _cf_account_fetched:
        return _cf_account_id
    _cf_account_fetched = True
    if not CF_TOKEN:
        return None  # no token -> no CF at all (guarantees zero CF calls)
    data, status = _cf_get(CF_API + "/accounts")
    _cf_accounts_status = status
    try:
        if isinstance(data, dict) and data.get("success") and data.get("result"):
            _cf_account_id = data["result"][0]["id"]
        else:
            _cf_account_id = None
    except Exception:
        _cf_account_id = None
    if not _cf_account_id:
        sys.stderr.write("CF: /accounts -> HTTP %s (no account resolved; "
                         "token auth/scope problem?)\n" % status)
    return _cf_account_id


def cf_previews_for(name):
    """Map of {branch: preview_url} for the repo's CF Pages project.

    Project name = repo basename lowercased with '_' -> '-'. Keeps the most
    recent (max created_on) preview url per branch. 404 / any error -> {}.
    """
    acct = cf_account_id()
    if not acct:
        return {}
    project = name.lower().replace("_", "-")
    url = "%s/accounts/%s/pages/projects/%s/deployments?per_page=100" % (
        CF_API, acct, project)
    data, status = _cf_get(url)
    if not (isinstance(data, dict) and data.get("success")
            and isinstance(data.get("result"), list)):
        # Categorize the failure so the run log is self-diagnosing.
        if status == 404:
            global _cf_proj_404
            _cf_proj_404 += 1
        elif status in (401, 403):
            global _cf_proj_auth
            _cf_proj_auth += 1
        else:
            global _cf_proj_other
            _cf_proj_other += 1
        return {}
    global _cf_proj_found
    _cf_proj_found += 1
    latest = {}
    for d in data["result"]:
        if d.get("environment") != "preview":
            continue
        branch = d.get("branch")
        du = d.get("url")
        created = d.get("created_on", "")
        if not branch or not du:
            continue
        cur = latest.get(branch)
        if cur is None or created > cur[1]:
            latest[branch] = (du, created)
    return {b: u for b, (u, _) in latest.items()}


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


def fetch_readme_title(repo):
    """Best-effort first ``# `` markdown heading from the repo's README, or None.

    ``GET /repos/{org}/{repo}/readme?ref={default_branch}`` (Accept:
    application/vnd.github+json) -> base64-decode ``content`` -> first line
    matching ``^#\\s+`` -> strip the leading ``# ``. A 404 / missing README / no
    ``# `` heading all yield None (the subtitle is simply omitted).
    """
    name = repo["name"]
    db = repo["default_branch"]
    try:
        # Best-effort: any failure (any non-200, network blip, empty repo, ...)
        # -> no title. One quick retry, then give up; never crash the run.
        data = _request("%s/repos/%s/%s/readme?ref=%s" % (API, ORG, name, db),
                        none_on=(404,), attempts=2)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    content = data.get("content")
    if not content:
        return None
    try:
        if data.get("encoding", "base64") == "base64":
            text = base64.b64decode(content).decode("utf-8", "replace")
        else:
            text = content
    except Exception:
        return None
    for line in text.splitlines():
        m = re.match(r"^#\s+(.*)$", line)
        if m:
            title = m.group(1).strip()
            return title or None
    return None


def _stable_site(name, tag):
    """GitHub URL for a tag: /releases/tag/ for release-looking names, else /tags/."""
    if tag.startswith("v") or tag.startswith("legacy"):
        return "https://github.com/%s/%s/releases/tag/%s" % (ORG, name, tag)
    return "https://github.com/%s/%s/tags/%s" % (ORG, name, tag)


def site_for_ref(repo_name, ref, cf_previews):
    """Website URL for a ref, or None.

    ``main`` -> the stable GitHub Pages site (reflects main). Any other ref ->
    a Cloudflare Pages preview URL (from ``cf_previews``) if one exists, else
    None.
    """
    if ref == "main":
        return "%s/%s/" % (GITHUB_IO_BASE, repo_name)
    return cf_previews.get(ref)


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
.title { margin:2px 0 0; font-size:.95rem; color:var(--ink); font-weight:600; }
.desc { margin:4px 0 0; font-size:.86rem; color:var(--muted); }
.topics { margin:6px 0 0; }
.pill { display:inline-block; background:var(--bg); color:var(--ink);
  border:1px solid var(--line); border-radius:10px; font-size:.7rem; padding:0 7px;
  margin:0 4px 2px 0; vertical-align:middle; }
.pill.divergent { background:#fff1e5; color:var(--accent2); border-color:#ffd8b3;
  margin-left:6px; }
a.ref { color:var(--accent); text-decoration:none; }
a.ref:hover { text-decoration:underline; }
.nosite { color:var(--muted); font-size:.75rem; }
"""


def _render_card(name, r):
    esc = html.escape
    stable = r["stable"]
    trunk = r["trunk"]
    branches = r["branches"]

    # 6. Stable (last tag) -> GitHub tag link
    if stable:
        stable_html = (
            '<a class="ref" href="%s"><code>%s</code> &rarr; <code>%s</code></a> '
            '&middot; <code>%s</code> &middot; %s'
            % (esc(r["stable_site"]), esc(stable["tag"]), esc(_short(stable["sha"])),
               esc(_date10(stable["date"])), esc(stable["subject"])))
    else:
        stable_html = '<span class="none">&mdash; (no tags)</span>'

    # 7. Trunk (current) -> site link (GitHub Pages for main / CF preview / none)
    if trunk is None:
        trunk_html = '<span class="none">&mdash; (empty repo, no commits)</span>'
    else:
        if r.get("trunk_site"):
            trunk_ref = (
                '<a class="ref" href="%s"><code>%s</code> &rarr; <code>%s</code></a>'
                % (esc(r["trunk_site"]), esc(trunk["branch"]), esc(_short(trunk["sha"]))))
        else:
            trunk_ref = (
                '<code>%s</code> &rarr; <code>%s</code> <span class="nosite">no site</span>'
                % (esc(trunk["branch"]), esc(_short(trunk["sha"]))))
        trunk_html = "%s &middot; <code>%s</code> &middot; %s" % (
            trunk_ref, esc(_date10(trunk["date"])), esc(trunk["subject"]))

    # 8. Feature heads -> site link or plain text (no link)
    if branches:
        items = []
        for b in branches:
            if b["ahead"] is None:
                div = " &middot; n/a (no common ancestor)"
                pill = ""
            else:
                div = " &middot; +%d/&minus;%d vs trunk" % (b["ahead"], b["behind"])
                pill = ' <span class="pill divergent">divergent</span>' if b["ahead"] > 0 else ""
            if b.get("site"):
                ref = (
                    '<a class="ref" href="%s"><code>%s</code> &rarr; <code>%s</code></a>'
                    % (esc(b["site"]), esc(b["name"]), esc(_short(b["sha"]))))
            else:
                ref = '<code>%s</code> &rarr; <code>%s</code>' % (
                    esc(b["name"]), esc(_short(b["sha"])))
            items.append("<li>%s%s%s</li>" % (ref, div, pill))
        features_block = (
            '<div class="row"><span class="label">Feature heads:</span></div>'
            '<ul class="features">%s</ul>' % "".join(items))
    else:
        features_block = (
            '<div class="row"><span class="label">Feature heads:</span> '
            '<span class="none">&mdash; (none)</span></div>')

    # 1-5. header block
    parts = []
    parts.append('<section class="card">')
    parts.append('  <h2><a href="https://github.com/%s/%s">%s</a></h2>'
                 % (esc(ORG), esc(name), esc(name)))
    if r.get("readme_title"):
        parts.append('  <p class="title">%s</p>' % esc(r["readme_title"]))
    if r.get("description"):
        parts.append('  <p class="desc">%s</p>' % esc(r["description"]))
    topics = r.get("topics") or []
    if topics:
        parts.append('  <div class="topics">%s</div>' % (
            "".join('<span class="pill">%s</span>' % esc(t) for t in topics)))
    parts.append(
        '  <div class="live"><a href="%s/%s/" target="_blank" rel="noopener">Live site &rarr;</a></div>'
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

    def _group(prefix):
        return sorted(n for n in repos if n.startswith(prefix))

    notebook = _group("notebook-")
    lecture = _group("lecture-")
    sample = _group("sample-")
    demo = _group("demo-")
    other = sorted(n for n in repos
                   if n not in notebook and n not in lecture
                   and n not in sample and n not in demo)

    groups = []
    if notebook:
        groups.append(("notebooks", notebook))
    if lecture:
        groups.append(("lectures", lecture))
    if sample:
        groups.append(("samples", sample))
    if demo:
        groups.append(("demos", demo))
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
    global TOKEN, CF_TOKEN
    TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
    if not TOKEN:
        sys.stderr.write("ERROR: GITHUB_TOKEN environment variable is empty\n")
        return 1
    CF_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()

    repos = list_repos()
    kept = [r for r in repos
            if r["name"].startswith(PREFIXES) and not r.get("archived")]
    kept.sort(key=lambda r: r["name"])

    data = {}
    for r in kept:
        name = r["name"]
        sys.stderr.write("Processing %s ...\n" % name)
        # CF Pages preview lookup: {} locally (no CF_API_TOKEN) -> non-main
        # refs show no link; in CI it maps branch -> preview url.
        cf_previews = cf_previews_for(name) if CF_TOKEN else {}
        stable = fetch_stable(r)
        trunk = fetch_trunk(r)
        branches = fetch_branches(r)
        stable_site = _stable_site(name, stable["tag"]) if stable else None
        trunk_site = site_for_ref(name, r["default_branch"], cf_previews) if trunk else None
        for b in branches:
            b["site"] = site_for_ref(name, b["name"], cf_previews)
        data[name] = {
            "default_branch": r["default_branch"],
            "readme_title": fetch_readme_title(r),
            "description": r.get("description"),
            "topics": r.get("topics") or [],
            "stable": stable,
            "stable_site": stable_site,
            "trunk": trunk,
            "trunk_site": trunk_site,
            "branches": branches,
        }

    if CF_TOKEN:
        total_previews = sum(
            len([b for b in r["branches"] if b.get("site")]) for r in data.values())
        sys.stderr.write(
            "CF: account=%s (accounts HTTP %s) | cf_api_calls=%d | "
            "projects: found=%d not_found_404=%d auth_err_401_403=%d other_err=%d "
            "| branches_with_site=%d\n"
            % ((cf_account_id() or "NONE"), _cf_accounts_status, _cf_call_count,
               _cf_proj_found, _cf_proj_404, _cf_proj_auth, _cf_proj_other,
               total_previews))

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

    readme_missing = sum(1 for r in data.values() if not r.get("readme_title"))
    sys.stderr.write(
        "Wrote %s/index.html and %s/manifest.json (%d repos)\n"
        % (OUT_DIR, OUT_DIR, len(data)))
    print("Total GitHub API requests: %d" % _call_count)
    if CF_TOKEN:
        print("Total Cloudflare API requests: %d" % _cf_call_count)
        print("CF account id: %s" % (cf_account_id() or "NONE"))
    else:
        print("CF account id: (no CF token -> no preview lookups)")
    print("Repos without README title (404/no-heading/skipped): %d" % readme_missing)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        sys.stderr.write("FATAL: %s\n" % e)
        sys.exit(1)
