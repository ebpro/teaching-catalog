#!/usr/bin/env python3
"""teaching-catalog generator.

Scans every ``notebook-*`` / ``lecture-*`` / ``sample-*`` / ``demo-*``
repository in the ``ebpro`` GitHub org and, for each one, assembles a
consistent, informative card:

  * a type icon (lecture / notebook / sample / demo)
  * the repo name (linked to GitHub) and its topic badges
  * the description
  * a links section with, for every repo:
      - Stable  -> the GitHub Pages site (when Pages is enabled)
      - develop -> the latest Cloudflare Pages preview deployment
      - features-> the Cloudflare Pages preview of every other branch
      - CI      -> the latest GitHub Actions run

Each Cloudflare Pages link carries the same deployment info the Pages
dashboard shows: environment, source branch, commit (short sha) and status.

Renders a self-contained static ``index.html`` plus a machine-readable
``manifest.json`` under ``./public/``.

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
PREFIXES = ("notebook-", "lecture-", "sample-", "demo-")
API = "https://api.github.com"
OUT_DIR = "public"
USER_AGENT = "ebpro-teaching-catalog"
GITHUB_IO_BASE = "https://ebpro.github.io"

PHILOSOPHY = (
    "This catalog presents a collection of interactive teaching materials "
    "\u2014 notebooks, lectures, samples, and demos \u2014 designed around a "
    "philosophy of learning by doing. Each course is a living repository "
    "where students clone, run, and modify real code. Continuous integration "
    "ensures every example is always up-to-date and working, so learners "
    "never start from a broken state. The system emphasizes practical "
    "skills: version control, containerization, and modern development "
    "workflows are not just taught but practiced from day one."
)

# --- API layer ------------------------------------------------------------

_call_count = 0
TOKEN = ""
_MAX_ATTEMPTS = 4
_BACKOFFS = (30, 60, 120)
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
# and no CF calls are made (branch rows fall back to GitHub links). Every CF
# failure is swallowed -> cf_deployments = {} so a Cloudflare problem can
# never break the (GitHub-backed) catalog.

CF_TOKEN = ""
CF_API = "https://api.cloudflare.com/client/v4"
_cf_call_count = 0
_cf_account_id = None
_cf_account_fetched = False
_cf_accounts_status = None
_cf_proj_found = 0
_cf_proj_404 = 0
_cf_proj_auth = 0
_cf_proj_other = 0
_cf_other_logged = False
_cf_other_body_logged = False
_cf_found_body_logged = False


def _cf_get(url):
    """GET a Cloudflare API url. Returns a (body, status) tuple:
    (parsed_json, 200) on success; (None, http_code) on an HTTP error;
    (None, -1) on any other failure (network, timeout, ...). Never raises."""
    global _cf_call_count, _cf_other_logged
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
        if not _cf_other_logged:
            _cf_other_logged = True
            try:
                body = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                body = "<unreadable>"
            sys.stderr.write("CF: HTTP %s (first) on %s: %s\n"
                             % (e.code, url.split("?")[0], body))
        return None, e.code
    except Exception as e:
        if not _cf_other_logged:
            _cf_other_logged = True
            sys.stderr.write("CF: non-HTTP error (first) on %s: %r\n"
                             % (url.split("?")[0], e))
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


def cf_deployments_for(name):
    """Map of ``{branch: deployment}`` for the repo's CF Pages project.

    Project name = repo basename lowercased with ``_`` -> ``-``. For each
    branch (both ``preview`` and ``production`` environments) keeps the most
    recent (max ``created_on``) deployment, capturing the fields needed to
    render the same info the CF Pages dashboard shows:

        url, environment, status, sha, commit_message, created_on

    404 / any error -> ``{}``.
    """
    acct = cf_account_id()
    if not acct:
        return {}
    project = name.lower().replace("_", "-")
    # per_page must be <= 20 for this endpoint: per_page=100 is rejected with
    # HTTP 400 / error 8000024. 20 is the documented default and is ample for
    # the latest deployment per branch.
    url = "%s/accounts/%s/pages/projects/%s/deployments?per_page=20" % (
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
            global _cf_proj_other, _cf_other_body_logged
            _cf_proj_other += 1
            if not _cf_other_body_logged:
                _cf_other_body_logged = True
                if isinstance(data, dict):
                    detail = "success=%r errors=%r result_type=%s" % (
                        data.get("success"), data.get("errors"),
                        type(data.get("result")).__name__)
                else:
                    detail = repr(data)[:400]
                sys.stderr.write(
                    "CF: other (status %s) project=%s | %s\n"
                    % (status, project, detail))
        return {}
    global _cf_proj_found, _cf_found_body_logged
    _cf_proj_found += 1
    if not _cf_found_body_logged:
        _cf_found_body_logged = True
        envs = {}
        for d in data["result"]:
            env = d.get("environment")
            envs[env] = envs.get(env, 0) + 1
        sys.stderr.write(
            "CF: first found project=%s deployments=%d environments=%r\n"
            % (project, len(data["result"]), envs))
    latest = {}
    for d in data["result"]:
        branch = d.get("branch")
        du = d.get("url")
        created = d.get("created_on", "")
        if not branch or not du:
            continue
        summary = d.get("summary") or {}
        git_info = summary.get("git_info") or {}
        dep = {
            "url": du,
            "environment": d.get("environment"),
            # Top-level status ("uploaded"/"in_progress"/"complete"/"error");
            # fall back to the summary status if the top-level one is absent.
            "status": d.get("status") or summary.get("status"),
            "sha": git_info.get("sha") or "",
            "commit_message": git_info.get("commit_message") or "",
            "created_on": created,
        }
        cur = latest.get(branch)
        if cur is None or created > cur["created_on"]:
            latest[branch] = dep
    return latest


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


def fetch_pages(repo):
    """GitHub Pages status for the repo, or None if Pages is not enabled.

    ``GET /repos/{org}/{repo}/pages`` -> 200 = enabled (returns ``html_url``
    + ``status``); 404 = not enabled (None). Any other error -> None.
    """
    name = repo["name"]
    try:
        data = _request("%s/repos/%s/%s/pages" % (API, ORG, name),
                        none_on=(404,), attempts=2)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "html_url": data.get("html_url")
        or "%s/%s/" % (GITHUB_IO_BASE, name),
        "status": data.get("status"),
    }


def fetch_latest_ci(repo):
    """Most recent GitHub Actions run for the repo, or None if none exists.

    ``GET /repos/{org}/{repo}/actions/runs?per_page=1`` -> first workflow run.
    Returns ``{status, conclusion, html_url, name, head_sha, head_branch,
    run_at}`` (each field may be None/"" when absent).
    """
    name = repo["name"]
    try:
        data = _request("%s/repos/%s/%s/actions/runs?per_page=1"
                        % (API, ORG, name), attempts=2)
    except Exception:
        return None
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    if not runs:
        return None
    run = runs[0]
    return {
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "html_url": run.get("html_url"),
        "name": run.get("name"),
        "head_sha": run.get("head_sha"),
        "head_branch": run.get("head_branch"),
        "run_at": run.get("run_at"),
    }


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
    """Every non-default branch name, sorted alphabetically.

    No per-branch compare calls (saves one API call per branch). The display
    only needs branch names, not divergence data.
    """
    name = repo["name"]
    db = repo["default_branch"]
    branches = _request("%s/repos/%s/%s/branches?per_page=100" % (API, ORG, name))
    return sorted(b["name"] for b in branches if b["name"] != db)


def fetch_readme_title(repo):
    """Best-effort first ``# `` markdown heading from the repo's README, or None.

    ``GET /repos/{org}/{repo}/readme?ref={default_branch}`` -> base64-decode
    ``content`` -> first line matching ``^#\\s+`` -> strip the leading ``# ``.
    A 404 / missing README / no ``# `` heading all yield None.
    """
    name = repo["name"]
    db = repo["default_branch"]
    try:
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


# --- link / deployment helpers -------------------------------------------

def _domain(url):
    """Strip the scheme from a URL for compact display."""
    if not url:
        return ""
    return re.sub(r"^https?://", "", url)


def _cf_status_emoji(status):
    """Map a CF Pages deployment status to a status glyph."""
    if status == "complete":
        return "\u2705"          # check mark
    if status in ("in_progress", "uploaded"):
        return "\U0001f504"      # cyclic arrows
    if status == "error":
        return "\u274c"          # cross mark
    return "\u2b55"              # white circle


def _ci_status_emoji(status, conclusion):
    """Map a GitHub Actions run (status, conclusion) to a status glyph."""
    if status == "completed":
        if conclusion == "success":
            return "\u2705"
        if conclusion == "failure":
            return "\u274c"
        return "\u26a0\ufe0f"    # warning (cancelled/skipped/neutral/...)
    if status in ("in_progress", "queued", "pending", "waiting"):
        return "\U0001f504"
    return "\u2b55"


def _dep_meta(dep):
    """Inner HTML for a CF deployment meta span: [env] status sha date."""
    esc = html.escape
    env = dep.get("environment")
    parts = []
    if env:
        parts.append("[%s]" % esc(env))
    parts.append(_cf_status_emoji(dep.get("status")))
    sha = _short(dep.get("sha"))
    if sha:
        parts.append(esc(sha))
    date = _date10(dep.get("created_on"))
    if date:
        parts.append(esc(date))
    return " ".join(parts)


def _branch_chip(branch, name, cf_deps):
    """A single branch link with its CF deployment meta, or a GitHub fallback."""
    esc = html.escape
    dep = cf_deps.get(branch)
    if dep and dep.get("url"):
        title = ' title="CF Pages %s deployment"' % esc(
            dep.get("environment") or "preview")
        chip = ('<span class="chip">'
                '<a class="ref-link" href="%s" target="_blank" '
                'rel="noopener"%s><code>%s</code></a>'
                % (esc(dep["url"]), title, esc(branch)))
        meta = _dep_meta(dep)
        if meta:
            chip += '<span class="dep-meta">%s</span>' % meta
        chip += "</span>"
        return chip
    gh_branch = "https://github.com/%s/%s/tree/%s" % (ORG, name, branch)
    return ('<span class="chip">'
            '<a class="ref-link" href="%s" target="_blank" '
            'rel="noopener"><code>%s</code></a></span>'
            % (esc(gh_branch), esc(branch)))


def _branch_row(label, branch, name, all_branches, cf_deps):
    """A full link-row for one named branch (e.g. ``develop``).

    Shows the CF Pages preview + deployment meta when available; falls back to
    a GitHub branch link when the branch exists but has no preview; a dash
    when the branch does not exist at all.
    """
    esc = html.escape
    dep = cf_deps.get(branch)
    if dep and dep.get("url"):
        title = ' title="CF Pages %s deployment"' % esc(
            dep.get("environment") or "preview")
        row = ('    <div class="link-row"><span class="link-label">%s</span> '
               '<a class="ref-link" href="%s" target="_blank" '
               'rel="noopener"%s><code>%s</code></a>'
               % (label, esc(dep["url"]), title, esc(branch)))
        meta = _dep_meta(dep)
        if meta:
            row += ' <span class="dep-meta">%s</span>' % meta
        row += "</div>"
        return row
    if branch in all_branches:
        gh_branch = "https://github.com/%s/%s/tree/%s" % (ORG, name, branch)
        return ('    <div class="link-row"><span class="link-label">%s</span> '
                '<a class="ref-link" href="%s" target="_blank" '
                'rel="noopener"><code>%s</code></a></div>'
                % (label, esc(gh_branch), esc(branch)))
    return ('    <div class="link-row"><span class="link-label">%s</span> '
            '<span class="none">&mdash;</span></div>' % label)


# --- HTML rendering -------------------------------------------------------

GITHUB_SVG = (
    '<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" '
    'aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 '
    '5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49'
    '-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 '
    '1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78'
    '-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12'
    '0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04'
    '2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07'
    '-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 '
    '.21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>'
)

# Consistent per-type icons (lectures vs notebooks vs samples vs demos).
TYPE_ICONS = {
    "lectures": "\U0001f4d8",    # blue book
    "notebooks": "\U0001f4d3",   # notebook
    "samples": "\U0001f9ea",     # test tube
    "demos": "\U0001f3ac",       # clapper board
    "other": "\U0001f4c1",       # file folder
}

CSS = """
:root {
  --bg: #f6f7f9; --card: #ffffff; --ink: #1f2328; --muted: #6a737d;
  --line: #e1e4e8; --accent: #0969da; --accent2: #bc4c00;
}
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; background: var(--bg); color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial,
  sans-serif; line-height: 1.5; }
.wrap { max-width: 900px; margin: 0 auto; }
h1 { font-size: 1.6rem; margin: 0 0 8px; }
.philosophy { color: var(--ink); font-size: .92rem; line-height: 1.6;
  margin: 0 0 12px; max-width: 720px; }
.meta { color: var(--muted); font-size: .85rem; margin-bottom: 20px; }
.meta a { color: var(--accent); text-decoration: none; }
.meta a:hover { text-decoration: underline; }

/* Tabs */
.tabs { display: flex; gap: 4px; margin-bottom: 16px;
  border-bottom: 2px solid var(--line); }
.tab-btn { background: none; border: none; padding: 8px 16px;
  font-size: .9rem; font-weight: 600; color: var(--muted); cursor: pointer;
  border-bottom: 2px solid transparent; margin-bottom: -2px;
  transition: color .15s, border-color .15s; }
.tab-btn:hover { color: var(--ink); }
.tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
.tab-content { display: none; }
.tab-content.active { display: block; }

/* Cards */
.card { background: var(--card); border: 1px solid var(--line);
  border-radius: 8px; padding: 14px 16px; margin-bottom: 12px;
  transition: opacity .15s; }
.card.legacy { opacity: .6; }
.card.legacy:hover { opacity: .85; }
.card h2 { font-size: 1.05rem; margin: 0 0 2px;
  display: flex; align-items: center; gap: 8px; }
.card h2 .title-link { color: var(--ink); text-decoration: none; flex: 1; }
.card h2 .title-link:hover { color: var(--accent); }
.icon-links { display: inline-flex; gap: 6px; flex-shrink: 0; }
.icon-link { color: var(--muted); display: inline-flex;
  align-items: center; }
.icon-link:hover { color: var(--accent); }
.icon-link svg { fill: currentColor; }

.desc { margin: 4px 0 0; font-size: .86rem; color: var(--muted); }
.badges { margin: 6px 0 0; }
.badge { display: inline-block; border: 1px solid; border-radius: 10px;
  font-size: .7rem; padding: 0 7px; margin: 0 4px 2px 0;
  vertical-align: middle; font-weight: 600; line-height: 1.7; }
.badge-area { background: #e7f0fd; color: #0a3069; border-color: #b6d0fe; }
.badge-status { background: #e6f4ea; color: #1a5632; border-color: #a7d9b8; }
.badge-status-legacy, .badge-status-stub { background: #f1f2f4;
  color: #57606a; border-color: #d0d4d9; }
.badge-status-frozen { background: #fff3e0; color: #9a4a00;
  border-color: #ffcc80; }
.badge-status-duplicate { background: #ffebe9; color: #a40e26;
  border-color: #ffcecb; }
.badge-fmt { background: #fff3e0; color: #e65100; border-color: #ffcc80; }
.badge-review { background: #f3e8ff; color: #6b21a8; border-color: #e3c7ff; }

/* Links section */
.links { margin-top: 10px; }
.link-row { margin: 5px 0; font-size: .88rem;
  display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.link-label { display: inline-block; min-width: 86px; color: var(--muted);
  font-weight: 600; flex-shrink: 0; }
.link-items { display: inline-flex; gap: 12px; flex-wrap: wrap; }
.chip { display: inline-flex; align-items: baseline; gap: 6px; }
.dep-meta { color: var(--muted); font-size: .76rem; white-space: nowrap; }
code { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
  monospace; font-size: .85em; background: var(--bg); border-radius: 4px;
  padding: 0 4px; }
.ref-link { color: var(--accent); text-decoration: none; }
.ref-link:hover { text-decoration: underline; }
.none { color: var(--muted); }

.type-icon { font-size: 1.05rem; line-height: 1; flex-shrink: 0; }
"""

JS = """
document.querySelectorAll('.tab-btn').forEach(function(btn) {
  btn.addEventListener('click', function() {
    var target = this.dataset.tab;
    document.querySelectorAll('.tab-btn').forEach(function(b) {
      b.classList.remove('active');
    });
    document.querySelectorAll('.tab-content').forEach(function(c) {
      c.classList.remove('active');
    });
    this.classList.add('active');
    document.getElementById('tab-' + target).classList.add('active');
  });
});
"""


def _is_legacy(topics):
    return "status-legacy" in topics


def _tab_for(name):
    """Determine which tab a repo belongs to based on its name prefix."""
    if name.startswith("lecture-"):
        return "lectures"
    if name.startswith("notebook-"):
        return "notebooks"
    if name.startswith("sample-"):
        return "samples"
    if name.startswith("demo-"):
        return "demos"
    return "other"


def _badges_html(topics):
    """Render ``area-*`` / ``status-*`` / ``fmt-*`` / ``review`` topics as
    small colored badges (other topics are ignored)."""
    out = []
    for t in topics:
        if t.startswith("area-"):
            label, cls = t[len("area-"):], "badge-area"
        elif t.startswith("status-"):
            value = t[len("status-"):]
            label = value
            cls = "badge-status badge-status-%s" % value
        elif t.startswith("fmt-"):
            label = "Jupyter" if t == "fmt-ipynb" else t[len("fmt-"):]
            cls = "badge-fmt"
        elif t == "review":
            label, cls = "review", "badge-review"
        else:
            continue
        out.append('<span class="badge %s">%s</span>'
                   % (cls, html.escape(label)))
    return "".join(out)


def _render_card(name, r):
    """Render a single repo card as an HTML string.

    Layout (consistent for every repo):
      [type-icon] Name (-> GitHub)        [github icon]
      description
      [area badge] [status badge] [fmt badge] [review badge]
      Links:
        \U0001f3e0 Stable   -> GitHub Pages site (when enabled)
        \U0001f33f develop  -> CF Pages preview + [env] status sha date
        \U0001f331 features -> CF Pages previews of every other branch
        \u2699\ufe0f CI        -> latest GitHub Actions run + sha branch date
    """
    esc = html.escape
    topics = r.get("topics") or []
    legacy = _is_legacy(topics)
    card_cls = "card legacy" if legacy else "card"

    tab = _tab_for(name)
    icon = TYPE_ICONS.get(tab, TYPE_ICONS["other"])
    title = r.get("readme_title") or name
    gh_url = "https://github.com/%s/%s" % (ORG, name)
    db = r.get("default_branch") or "main"
    cf_deps = r.get("cf_deployments") or {}
    pages = r.get("pages")
    ci = r.get("latest_ci")
    branches = r.get("branches") or []
    all_branches = set(branches) | {db}

    parts = []
    parts.append('<section class="%s">' % card_cls)

    # Header: type icon + name (linked to GitHub) + GitHub icon.
    parts.append('  <h2>')
    parts.append('    <span class="type-icon" aria-hidden="true">%s</span>'
                 % icon)
    parts.append('    <a class="title-link" href="%s">%s</a>'
                 % (esc(gh_url), esc(title)))
    parts.append('    <span class="icon-links">')
    parts.append(
        '      <a class="icon-link" href="%s" target="_blank" '
        'rel="noopener" title="GitHub">%s</a>'
        % (esc(gh_url), GITHUB_SVG))
    parts.append('    </span>')
    parts.append('  </h2>')

    # Description (if any)
    if r.get("description"):
        parts.append('  <p class="desc">%s</p>' % esc(r["description"]))

    # Topic badges (area / status / fmt / review)
    badges = _badges_html(topics)
    if badges:
        parts.append('  <div class="badges">%s</div>' % badges)

    # Links section
    parts.append('  <div class="links">')

    # Stable: GitHub Pages (if enabled).
    parts.append('    <div class="link-row">')
    parts.append('      <span class="link-label">\U0001f3e0 Stable</span>')
    if pages:
        parts.append(
            '      <a class="ref-link" href="%s" target="_blank" '
            'rel="noopener">%s</a>'
            % (esc(pages["html_url"]), esc(_domain(pages["html_url"]))))
    else:
        parts.append('      <span class="none">Pages not enabled</span>')
    parts.append('    </div>')

    # develop: CF Pages preview (or GitHub branch link / dash).
    parts.append(_branch_row("\U0001f33f develop", "develop", name,
                             all_branches, cf_deps))

    # Feature branches: CF Pages previews (or GitHub links).
    feature_branches = [b for b in sorted(branches)
                        if b != "gh-pages" and b != db]
    if feature_branches:
        chips = "".join(_branch_chip(b, name, cf_deps)
                        for b in feature_branches)
        parts.append('    <div class="link-row">')
        parts.append('      <span class="link-label">\U0001f331 features</span>')
        parts.append('      <span class="link-items">%s</span>' % chips)
        parts.append('    </div>')
    else:
        parts.append(
            '    <div class="link-row"><span class="link-label">'
            '\U0001f331 features</span> <span class="none">&mdash;</span>'
            '</div>')

    # CI: latest GitHub Actions run.
    parts.append('    <div class="link-row">')
    parts.append('      <span class="link-label">\u2699\ufe0f CI</span>')
    if ci and ci.get("html_url"):
        emoji = _ci_status_emoji(ci.get("status"), ci.get("conclusion"))
        label = ci.get("name") or "latest run"
        parts.append(
            '      <a class="ref-link" href="%s" target="_blank" '
            'rel="noopener">%s %s</a>'
            % (esc(ci["html_url"]), emoji, esc(label)))
        meta_bits = [x for x in (_short(ci.get("head_sha")),
                                 esc(ci.get("head_branch") or ""),
                                 esc(_date10(ci.get("run_at")))) if x]
        if meta_bits:
            parts.append('      <span class="dep-meta">%s</span>'
                         % " &middot; ".join(meta_bits))
    else:
        parts.append('      <span class="none">&mdash;</span>')
    parts.append('    </div>')

    parts.append('  </div>')
    parts.append('</section>')
    return "\n".join(parts)


def _render_html(manifest):
    """Render the full index.html from the manifest dict."""
    esc = html.escape
    repos = manifest["repos"]
    generated = esc(manifest["generated_at"])
    count = manifest["count"]

    # Group repos by tab
    tab_groups = {
        "lectures": [],
        "notebooks": [],
        "samples": [],
        "demos": [],
        "other": [],
    }
    for name in repos:
        tab_groups[_tab_for(name)].append(name)

    # Sort each tab: non-legacy first (alpha), then legacy (alpha)
    for tab in tab_groups:
        non_legacy = sorted(
            n for n in tab_groups[tab]
            if not _is_legacy(repos[n].get("topics") or []))
        legacy = sorted(
            n for n in tab_groups[tab]
            if _is_legacy(repos[n].get("topics") or []))
        tab_groups[tab] = non_legacy + legacy

    # Tab definitions in display order
    tab_defs = [
        ("lectures", "Lectures"),
        ("notebooks", "Notebooks"),
        ("samples", "Samples"),
        ("demos", "Demos"),
    ]
    if tab_groups["other"]:
        tab_defs.append(("other", "Other"))

    # Build tab buttons
    tab_buttons = []
    for i, (tab_id, tab_label) in enumerate(tab_defs):
        active = " active" if i == 0 else ""
        n = len(tab_groups[tab_id])
        tab_buttons.append(
            '<button class="tab-btn%s" data-tab="%s">%s (%d)</button>'
            % (active, tab_id, esc(tab_label), n))

    # Build tab content panes
    tab_contents = []
    for i, (tab_id, _label) in enumerate(tab_defs):
        active = " active" if i == 0 else ""
        names = tab_groups[tab_id]
        if names:
            cards = "\n".join(_render_card(n, repos[n]) for n in names)
        else:
            cards = '<p class="none">No repositories.</p>'
        tab_contents.append(
            '<div class="tab-content%s" id="tab-%s">\n%s\n</div>'
            % (active, tab_id, cards))

    parts = []
    parts.append("<!doctype html>")
    parts.append('<html lang="en">')
    parts.append("<head>")
    parts.append('<meta charset="utf-8">')
    parts.append('<meta name="viewport" '
                 'content="width=device-width, initial-scale=1">')
    parts.append("<title>Emmanuel BRUNO teaching catalog</title>")
    parts.append("<style>%s</style>" % CSS)
    parts.append("</head>")
    parts.append("<body>")
    parts.append('<div class="wrap">')
    parts.append("<h1>Emmanuel BRUNO teaching catalog</h1>")
    parts.append('<p class="philosophy">%s</p>' % esc(PHILOSOPHY))
    parts.append(
        '<div class="meta">Generated %s &middot; %d repos '
        '&middot; auto-updates every 6h &middot; '
        '<a href="https://github.com/ebpro/teaching-catalog">'
        'teaching-catalog</a></div>' % (generated, count))
    parts.append('<div class="tabs">\n%s\n</div>'
                 % "\n".join(tab_buttons))
    parts.extend(tab_contents)
    parts.append("</div>")
    parts.append("<script>%s</script>" % JS)
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
        # CF Pages deployments lookup: {} locally (no CF_API_TOKEN); in CI it
        # maps branch -> {url, environment, status, sha, created_on, ...}.
        cf_deps = cf_deployments_for(name) if CF_TOKEN else {}
        trunk = fetch_trunk(r)
        branches = fetch_branches(r)
        db = r["default_branch"]
        data[name] = {
            "default_branch": db,
            "readme_title": fetch_readme_title(r),
            "description": r.get("description"),
            "topics": r.get("topics") or [],
            "trunk": trunk,
            "branches": branches,
            "pages": fetch_pages(r),
            "latest_ci": fetch_latest_ci(r),
            "cf_deployments": cf_deps,
        }

    if CF_TOKEN:
        sys.stderr.write(
            "CF: account=%s (accounts HTTP %s) | cf_api_calls=%d | "
            "projects: found=%d not_found_404=%d auth_err_401_403=%d "
            "other_err=%d\n"
            % ((cf_account_id() or "NONE"), _cf_accounts_status,
               _cf_call_count, _cf_proj_found, _cf_proj_404,
               _cf_proj_auth, _cf_proj_other))

    manifest = {
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "org": ORG,
        "count": len(data),
        "repos": data,
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    with open(os.path.join(OUT_DIR, "index.html"), "w",
              encoding="utf-8") as f:
        f.write(_render_html(manifest))

    readme_missing = sum(1 for r in data.values()
                         if not r.get("readme_title"))
    sys.stderr.write(
        "Wrote %s/index.html and %s/manifest.json (%d repos)\n"
        % (OUT_DIR, OUT_DIR, len(data)))
    print("Total GitHub API requests: %d" % _call_count)
    if CF_TOKEN:
        print("Total Cloudflare API requests: %d" % _cf_call_count)
        print("CF account id: %s" % (cf_account_id() or "NONE"))
    else:
        print("CF account id: (no CF token -> no preview lookups)")
    print("Repos without README title (404/no-heading/skipped): %d"
          % readme_missing)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        sys.stderr.write("FATAL: %s\n" % e)
        sys.exit(1)
