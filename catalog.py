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

DASHBOARD_CSS = """
:root {
  --color-bg: #f8fafc;
  --color-surface: #ffffff;
  --color-text: #1e293b;
  --color-text-muted: #64748b;
  --color-primary: #3b82f6;
  --color-primary-hover: #2563eb;
  --color-success: #22c55e;
  --color-error: #ef4444;
  --color-warning: #f59e0b;
  --color-border: #e2e8f0;
  --radius: 12px;
  --shadow-sm: 0 1px 3px rgba(0,0,0,0.08);
  --shadow-md: 0 4px 12px rgba(0,0,0,0.12);
  --max-width: 1200px;
  /* legacy aliases (kept for existing rules) */
  --bg-dark: #1a1a2e;
  --bg-page: var(--color-bg);
  --bg-card: var(--color-surface);
  --text: var(--color-text);
  --text-muted: var(--color-text-muted);
  --accent: var(--color-primary);
  --success: var(--color-success);
  --error: var(--color-error);
  --warning: var(--color-warning);
  --shadow: var(--shadow-sm);
  --shadow-hover: var(--shadow-md);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, sans-serif; background: var(--bg-page); color: var(--text); line-height: 1.6; overflow-x: hidden; }
.card-title a { overflow-wrap: break-word; word-break: break-all; }

/* Header */
header { position: sticky; top: 0; z-index: 100; background: var(--bg-dark); color: white; padding: 16px 24px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
header h1 { font-size: 1.3rem; font-weight: 600; }
header h1 span { opacity: 0.7; font-weight: 400; }
.search { flex: 1; min-width: 200px; max-width: 400px; padding: 8px 16px; border-radius: 20px; border: none; background: rgba(255,255,255,0.15); color: white; font-size: 0.9rem; }
.search::placeholder { color: rgba(255,255,255,0.5); }
.search:focus { outline: none; background: rgba(255,255,255,0.25); }
.toggle { display: flex; border-radius: 20px; overflow: hidden; border: 1px solid rgba(255,255,255,0.3); }
.toggle button { padding: 6px 14px; border: none; background: transparent; color: white; cursor: pointer; font-size: 0.85rem; transition: all 0.2s; }
.toggle button.active { background: var(--accent); }

/* Stats bar (teacher) */
#stats-bar { display: flex; gap: 12px; padding: 12px 24px; background: white; border-bottom: 1px solid #e0e0e0; flex-wrap: wrap; }
.stat { padding: 4px 12px; border-radius: 16px; font-size: 0.8rem; font-weight: 500; cursor: pointer; transition: all 0.2s; }
.stat:hover { transform: translateY(-1px); }
.stat.total { background: #dfe6e9; }
.stat.green { background: #55efc4; color: #00b894; }
.stat.red { background: #fab1a0; color: #d63031; }
.stat.yellow { background: #ffeaa7; color: #fdcb6e; }
.stat.time { background: #dfe6e9; cursor: default; margin-left: auto; }

/* Filter chips */
#filter-chips { display: flex; gap: 8px; padding: 12px 24px; flex-wrap: wrap; }
.chip { padding: 6px 14px; border-radius: 16px; border: 1px solid #dfe6e9; background: white; font-size: 0.8rem; cursor: pointer; transition: all 0.2s; }
.chip.active { background: var(--accent); color: white; border-color: var(--accent); }
.chip:hover { border-color: var(--accent); }

/* Card grid */
#content { max-width: var(--max-width); margin: 0 auto; padding: 0 16px 24px; display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }

/* Teacher card */
.card { background: var(--bg-card); border-radius: var(--radius); box-shadow: var(--shadow); padding: 20px; transition: all 0.2s; position: relative; }
.card:hover { box-shadow: var(--shadow-hover); transform: translateY(-2px); }
.card-header { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.status-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.status-dot.green { background: var(--success); }
.status-dot.red { background: var(--error); }
.status-dot.yellow { background: var(--warning); }
.status-dot.gray { background: #b2bec3; }
.card-title { font-size: 0.9rem; font-weight: 600; font-family: 'JetBrains Mono', 'Fira Code', monospace; flex: 1; }
.card-title a { color: var(--text); text-decoration: none; }
.card-title a:hover { color: var(--accent); }
.ci-link { font-size: 1.1rem; text-decoration: none; }
.card-desc { font-size: 0.85rem; color: var(--text-muted); margin-bottom: 12px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.badges { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 12px; }
.badge { padding: 2px 8px; border-radius: 10px; font-size: 0.7rem; font-weight: 500; }
.badge.area { background: #74b9ff; color: white; }
.badge.status-active { background: #55efc4; color: #00695c; }
.badge.status-frozen { background: #fab1a0; color: #d63031; }
.badge.status-legacy { background: #dfe6e9; color: #636e72; }
.badge.topic { background: #f0f0f0; color: #636e72; }
.card-links { display: flex; gap: 12px; padding-top: 12px; border-top: 1px solid #f0f0f0; font-size: 0.8rem; }
.card-links a { color: var(--accent); text-decoration: none; min-height: 44px; display: inline-flex; align-items: center; }
.card-links a:hover { text-decoration: underline; }
.card-branches { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--color-border); }
.branch-link { font-size: 11px; color: var(--color-text-muted); text-decoration: none; padding: 2px 6px; background: var(--color-bg); border-radius: 4px; border: 1px solid var(--color-border); }
.branch-link:hover { color: var(--color-primary); border-color: var(--color-primary); }
.card-footer { margin-top: 8px; font-size: 0.75rem; color: var(--text-muted); }

/* Student card */
.card.student { padding: 28px; text-align: center; }
.card.student .type-icon { font-size: 2.5rem; margin-bottom: 12px; }
.card.student .card-title { font-size: 1.1rem; font-family: inherit; font-weight: 600; }
.card.student .card-desc { -webkit-line-clamp: 3; }
.cta-btn { display: inline-flex; align-items: center; justify-content: center; margin-top: 16px; padding: 12px 24px; min-height: 44px; background: var(--accent); color: white; border-radius: 20px; text-decoration: none; font-size: 0.9rem; font-weight: 500; transition: all 0.2s; }
.cta-btn:hover { background: #0769b8; transform: translateY(-1px); }
.cta-stable { background: var(--color-success); color: white; font-size: 16px; padding: 12px 24px; border-radius: 8px; font-weight: 600; }
.cta-stable:hover { background: #16a34a; }
.cta-dev { background: var(--color-primary); color: white; font-size: 14px; padding: 10px 20px; border-radius: 8px; font-weight: 500; position: relative; }
.cta-dev::before { content: 'DEV'; position: absolute; top: -6px; right: -6px; background: #fbbf24; color: #78350f; font-size: 9px; font-weight: 700; padding: 2px 5px; border-radius: 4px; }
.cta-dev:hover { background: var(--color-primary-hover); }
.cta-github { background: transparent; color: var(--color-text-muted); border: 1px solid var(--color-border); font-size: 14px; padding: 8px 16px; border-radius: 8px; }
.cta-github:hover { border-color: var(--color-primary); color: var(--color-primary); }

/* Student section headers */
.section-header { grid-column: 1 / -1; padding: 20px 0 8px; font-size: 1.2rem; font-weight: 600; border-bottom: 2px solid var(--accent); margin-bottom: 4px; }

/* Responsive */
@media (max-width: 768px) {
  #content { grid-template-columns: 1fr; padding: 0 16px 16px; }
  header { flex-direction: column; gap: 12px; padding: 12px 16px; }
  .search { max-width: 100%; width: 100%; order: 3; }
  #filter-chips { flex-wrap: wrap; }
  body { font-size: 16px; }
  #stats-bar { padding: 12px 16px; }
}
@media (max-width: 1024px) and (min-width: 769px) {
  #content { grid-template-columns: repeat(2, 1fr); }
}

/* Reduced motion */
@media (prefers-reduced-motion: reduce) {
  .card { transition: none; }
  .card:hover { transform: none; }
}
"""

DASHBOARD_JS = """
const DATA = __DATA_JSON__;
const TITLES = __TITLES_JSON__;
let currentView = 'student'; // 'teacher' | 'student'
let currentFilter = 'all';
let searchQuery = '';
let _loaded = false;

function getTypeIcon(type) {
  return { lecture: '\U0001f4d8', notebook: '\U0001f4d3', sample: '\U0001f9ea', demo: '\U0001f3ac' }[type] || '\U0001f4c1';
}

function getStatusDot(repo) {
  if (repo.ci_status === 'success') return 'green';
  if (repo.ci_status === 'failure') return 'red';
  return 'gray';
}

function relativeTime(dateStr) {
  if (!dateStr) return '';
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return mins + ' min';
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours + 'h';
  const days = Math.floor(hours / 24);
  return days + 'j';
}

function getDisplayTitle(repo) {
  return TITLES[repo.name] || repo.name.replace(/^(lecture|notebook|sample|demo)-/, '').replace(/-/g, ' ');
}

function render() {
  const main = document.getElementById('content');
  const statsBar = document.getElementById('stats-bar');
  const filterChips = document.getElementById('filter-chips');

  // Filter repos
  let repos = DATA.repos.filter(r => {
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      if (!r.name.toLowerCase().includes(q) && !(r.description||'').toLowerCase().includes(q) && !(r.topics||[]).some(t => t.includes(q))) return false;
    }
    if (currentFilter === 'lecture' && r.type !== 'lecture') return false;
    if (currentFilter === 'notebook' && r.type !== 'notebook') return false;
    if (currentFilter === 'failed' && r.ci_status !== 'failure') return false;
    if (currentFilter === 'recent' && (!r.updated_at || Date.now() - new Date(r.updated_at).getTime() > 86400000)) return false;
    if (currentView === 'student' && !r.topics?.includes('status-active')) return false;
    return true;
  });

  // Sort: teacher = failed first then by date; student = by name
  if (currentView === 'teacher') {
    const order = { failure: 0, in_progress: 1, queued: 2, success: 3, null: 4 };
    repos.sort((a, b) => (order[a.ci_status]||4) - (order[b.ci_status]||4) || new Date(b.updated_at||0) - new Date(a.updated_at||0));
  } else {
    repos.sort((a, b) => a.type.localeCompare(b.type) || a.name.localeCompare(b.name));
  }

  // Update stats (teacher only)
  if (currentView === 'teacher') {
    statsBar.style.display = 'flex';
    const total = DATA.repos.length;
    const green = DATA.repos.filter(r => r.ci_status === 'success').length;
    const red = DATA.repos.filter(r => r.ci_status === 'failure').length;
    statsBar.innerHTML = `
      <span class="stat total" data-filter="all">\U0001f4e6 ${total} repos</span>
      <span class="stat green" data-filter="green">\u2705 ${green} green</span>
      <span class="stat red" data-filter="failed">\u274c ${red} erreurs</span>
      <span class="stat time">\U0001f550 ${new Date(DATA.generated_at).toLocaleDateString('fr-FR')}</span>
    `;
  } else {
    statsBar.style.display = 'none';
  }

  // Render cards
  if (currentView === 'student') {
    // Group by type with section headers
    const lectures = repos.filter(r => r.type === 'lecture');
    const notebooks = repos.filter(r => r.type !== 'lecture');
    let html = '';
    if (lectures.length) {
      html += '<div class="section-header">\U0001f4d8 Cours</div>';
      html += lectures.map(r => studentCard(r)).join('');
    }
    if (notebooks.length) {
      html += '<div class="section-header">\U0001f4d3 Notebooks & Pratiques</div>';
      html += notebooks.map(r => studentCard(r)).join('');
    }
    main.innerHTML = html;
  } else {
    main.innerHTML = repos.map(r => teacherCard(r)).join('');
  }

  // Loading / empty state management
  if (!_loaded) {
    document.getElementById('loading').style.display = 'none';
    _loaded = true;
  }
  if (repos.length === 0) {
    main.style.display = 'none';
    document.getElementById('empty').style.display = 'block';
  } else {
    main.style.display = 'grid';
    document.getElementById('empty').style.display = 'none';
  }
}

function teacherCard(r) {
  const dot = getStatusDot(r);
  const ciEmoji = { success: '\u2705', failure: '\u274c', in_progress: '\U0001f504', queued: '\u23f3' }[r.ci_status] || '\u00b7';
  const badges = (r.topics||[]).map(t => {
    const cls = t.startsWith('area-') ? 'area' : t.startsWith('status-') ? t : 'topic';
    return `<span class="badge ${cls}">${t.replace(/^(area|status|fmt)-/, '')}</span>`;
  }).join('');
  let html = `<article class="card">
    <div class="card-header">
      <span class="status-dot ${dot}"></span>
      <span class="card-title">${getTypeIcon(r.type)} <a href="${r.github_url}" target="_blank">${r.name}</a></span>
      <a class="ci-link" href="${r.ci_url||'#'}" title="CI: ${r.ci_status||'n/a'}">${ciEmoji}</a>
    </div>
    <p class="card-desc">${r.description||''}</p>
    <div class="badges">${badges}</div>
    <div class="card-links">
      ${r.stable_url ? `<a href="${r.stable_url}" target="_blank">\U0001f4c4 View</a>` : ''}
      ${r.develop_url ? `<a href="${r.develop_url}" target="_blank">\U0001f33f dev</a>` : ''}
      ${r.ci_url ? `<a href="${r.ci_url}" target="_blank">\u2699\ufe0f CI</a>` : ''}
      ${r.cf_url ? `<a href="${r.cf_url}" target="_blank">\u2601\ufe0f CF</a>` : ''}
    </div>`;
  const branchLinks = (r.branches || [])
    .filter(b => b !== r.default_branch)
    .slice(0, 5)
    .map(b => `<a class="branch-link" href="${r.github_url}/tree/${encodeURIComponent(b)}" title="${b}">${b.startsWith('feature/') ? '\u2728' : b.startsWith('fix/') ? '\U0001f527' : '\U0001f33f'} ${b.replace(/^(feature|fix|hotfix)\\//, '')}</a>`)
    .join('');
  if (branchLinks) {
    html += `<div class="card-branches">${branchLinks}</div>`;
  }
  html += `<div class="card-footer">${r.updated_at ? 'Mis \u00e0 jour: ' + relativeTime(r.updated_at) : ''}</div>
  </article>`;
  return html;
}

function studentCard(r) {
  let cta;
  if (r.stable_url) {
    cta = `<a class="cta-btn cta-stable" href="${r.stable_url}" target="_blank">\U0001f4d6 Consulter le cours</a>`;
  } else if (r.develop_url || r.cf_url) {
    cta = `<a class="cta-btn cta-dev" href="${r.develop_url || r.cf_url}" target="_blank">\U0001f6a7 Version dev (pas encore de version stable)</a>`;
  } else {
    cta = `<a class="cta-btn cta-github" href="${r.github_url}" target="_blank">\U0001f4bb Voir sur GitHub</a>`;
  }
  return `<article class="card student">
    <div class="type-icon">${getTypeIcon(r.type)}</div>
    <div class="card-title">${getDisplayTitle(r)}</div>
    <p class="card-desc">${r.description||''}</p>
    ${cta}
  </article>`;
}

// Event listeners
document.getElementById('search').addEventListener('input', e => { searchQuery = e.target.value; render(); });
document.querySelectorAll('.toggle button').forEach(btn => btn.addEventListener('click', () => {
  currentView = btn.dataset.view;
  document.querySelectorAll('.toggle button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  render();
}));
document.getElementById('filter-chips').addEventListener('click', e => {
  const chip = e.target.closest('.chip');
  if (!chip) return;
  currentFilter = chip.dataset.filter;
  document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
  chip.classList.add('active');
  render();
});
document.getElementById('stats-bar').addEventListener('click', e => {
  const stat = e.target.closest('.stat');
  if (!stat || !stat.dataset.filter) return;
  currentFilter = stat.dataset.filter === 'all' ? 'all' : stat.dataset.filter === 'green' ? 'all' : stat.dataset.filter;
  // Map green\u2192all (show all), failed\u2192failed, yellow\u2192all
  if (stat.dataset.filter === 'green') currentFilter = 'all';
  document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
  const matching = document.querySelector(`.chip[data-filter="${currentFilter}"]`);
  if (matching) matching.classList.add('active');
  render();
});

// Initial render
render();
"""


def _repo_type(name):
    """Derive the display type from the repo name prefix."""
    if name.startswith("lecture-"):
        return "lecture"
    if name.startswith("notebook-"):
        return "notebook"
    if name.startswith("sample-"):
        return "sample"
    return "demo"


def _ci_status_normalized(ci):
    """Normalize a latest_ci dict to a single status string for the JS layer.

    Returns "success", "failure", "in_progress", "queued", or None.
    """
    if not ci:
        return None
    status = ci.get("status")
    conclusion = ci.get("conclusion")
    if status == "completed":
        if conclusion == "success":
            return "success"
        if conclusion == "failure":
            return "failure"
        return None  # cancelled / skipped / neutral / stale
    if status in ("in_progress", "queued"):
        return status
    return None


def _build_data(manifest):
    """Build the DATA object embedded in the dashboard HTML."""
    repos_data = []
    for name, repo in sorted(manifest["repos"].items()):
        pages = repo.get("pages")
        stable_url = pages["html_url"] if pages else None

        cf_deps = repo.get("cf_deployments") or {}
        develop_dep = cf_deps.get("develop")
        develop_url = (develop_dep["url"]
                       if develop_dep and develop_dep.get("url") else None)

        cf_url = None
        if cf_deps:
            cf_url = "https://%s.pages.dev" % name.lower().replace("_", "-")

        ci = repo.get("latest_ci")
        ci_status = _ci_status_normalized(ci)
        ci_url = ci.get("html_url") if ci else None

        trunk = repo.get("trunk")
        updated_at = trunk["date"] if trunk else None

        repos_data.append({
            "name": name,
            "type": _repo_type(name),
            "description": repo.get("description") or "",
            "topics": repo.get("topics") or [],
            "stable_url": stable_url,
            "develop_url": develop_url,
            "cf_url": cf_url,
            "ci_status": ci_status,
            "ci_url": ci_url,
            "updated_at": updated_at,
            "default_branch": repo.get("default_branch"),
            "branches": repo.get("branches") or [],
            "github_url": "https://github.com/%s/%s" % (ORG, name),
        })
    return {
        "generated_at": manifest["generated_at"],
        "repos": repos_data,
    }


def _build_titles(manifest):
    """Build the TITLES mapping (repo name -> README title) for the JS layer."""
    titles = {}
    for name, repo in manifest["repos"].items():
        title = repo.get("readme_title")
        if title:
            titles[name] = title
    return titles


def _safe_json(obj):
    """Serialize to JSON safe for embedding in a <script> tag.

    Escapes ``</`` sequences to prevent premature ``</script>`` closure.
    """
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def _render_html(manifest):
    """Render the full dual-view dashboard index.html from the manifest dict."""
    data_json = _safe_json(_build_data(manifest))
    titles_json = _safe_json(_build_titles(manifest))

    js = (DASHBOARD_JS
          .replace("__DATA_JSON__", data_json)
          .replace("__TITLES_JSON__", titles_json))

    # Format the generation date for the footer
    gen_at = manifest.get("generated_at", "")
    try:
        from datetime import datetime as _dt
        _d = _dt.fromisoformat(gen_at.replace("Z", "+00:00"))
        date_str = _d.strftime("%d %B %Y")
    except Exception:
        date_str = gen_at[:10] if gen_at else ""

    return (
        '<!DOCTYPE html>\n'
        '<html lang="fr">\n'
        '<head>\n'
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '  <title>ebpro \u2014 Plateforme d\u2019enseignement</title>\n'
        '  <style>%s</style>\n'
        '</head>\n'
        '<body>\n'
        '  <header>\n'
        '    <h1>\U0001f4da ebpro <span>\u2014 Plateforme d\u2019enseignement</span></h1>\n'
        '    <input type="search" id="search" class="search" '
        'placeholder="Rechercher un cours..." aria-label="Rechercher">\n'
        '    <div class="toggle" role="tablist">\n'
        '      <button data-view="teacher" role="tab">'
        '\U0001f468\u200d\U0001f3eb Enseignant</button>\n'
        '      <button class="active" data-view="student" role="tab">'
        '\U0001f393 \u00c9tudiant</button>\n'
        '    </div>\n'
        '  </header>\n'
        '  <div id="stats-bar"></div>\n'
        '  <div id="filter-chips">\n'
        '    <span class="chip active" data-filter="all">Tous</span>\n'
        '    <span class="chip" data-filter="lecture">\U0001f4d8 Cours</span>\n'
        '    <span class="chip" data-filter="notebook">\U0001f4d3 Notebooks</span>\n'
        '    <span class="chip" data-filter="failed">\u274c Erreurs</span>\n'
        '    <span class="chip" data-filter="recent">\U0001f550 R\u00e9cents (24h)</span>\n'
        '  </div>\n'
        '  <div id="loading" style="text-align:center;padding:60px 20px;color:#64748b;">\n'
        '    <div style="font-size:32px;margin-bottom:12px;">\u23f3</div>\n'
        '    Chargement du catalogue\u2026\n'
        '  </div>\n'
        '  <main id="content" style="display:none"></main>\n'
        '  <div id="empty" style="display:none;text-align:center;padding:60px 20px;color:#64748b;">\n'
        '    <div style="font-size:32px;margin-bottom:12px;">\U0001f50d</div>\n'
        '    Aucun r\u00e9sultat trouv\u00e9\n'
        '  </div>\n'
        '  <footer style="text-align:center;padding:32px 16px;color:#94a3b8;font-size:13px;border-top:1px solid #e2e8f0;margin-top:48px;">\n'
        '    G\u00e9n\u00e9r\u00e9 le %s \u00b7 <a href="https://github.com/ebpro" style="color:#3b82f6;">ebpro</a>\n'
        '  </footer>\n'
        '  <script>%s</script>\n'
        '</body>\n'
        '</html>\n'
        % (DASHBOARD_CSS, date_str, js)
    )


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
