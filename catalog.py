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
GraphQL API (bulk repo metadata: trunk head, branches, topics, description) plus
the GitHub REST API (Pages / CI / README, which have no GraphQL equivalent) and,
when ``CF_API_TOKEN`` is set, the Cloudflare Pages API (best-effort: a CF failure
never breaks the catalog). No threads (all calls are strictly sequential so the
rate limit is respected).

Auth: reads ``GITHUB_TOKEN`` (required) and ``CF_API_TOKEN`` (optional) from the
environment.
"""

import argparse
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
GRAPHQL_API = "https://api.github.com/graphql"
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

# --- proactive rate-limit tracking ----------------------------------------
# GitHub reports the *primary* (5000/hour) limit on every response. We track
# it so we can pause *before* hitting the wall instead of eating 403s. The
# *secondary* (abuse/burst) limits -- the ones that actually triggered the
# fixed-30s sleeps -- are handled in _request via Retry-After + inter-repo
# pacing. Both globals are refreshed on every successful response.
_rate_limit_remaining = 5000  # initial assumption (overwritten by first response)
_rate_limit_reset = 0         # unix timestamp of the next primary-limit reset


def _check_rate_limit(headers):
    """Update the tracked rate-limit state and proactively throttle.

    Called on every successful response. If the primary limit is running low
    (< 50 calls remaining) we sleep until the reset window + 1s buffer so the
    next request starts in a fresh window instead of tripping a 403.
    """
    global _rate_limit_remaining, _rate_limit_reset
    remaining = headers.get("X-RateLimit-Remaining")
    reset = headers.get("X-RateLimit-Reset")
    if remaining is not None:
        try:
            _rate_limit_remaining = int(remaining)
        except (TypeError, ValueError):
            pass
    if reset is not None:
        try:
            _rate_limit_reset = int(reset)
        except (TypeError, ValueError):
            pass
    if _rate_limit_remaining < 50 and _rate_limit_reset > 0:
        wait = _rate_limit_reset - time.time() + 1
        if wait > 0:
            print("INFO: Rate limit low (%d remaining) - sleeping %.0fs "
                  "until reset" % (_rate_limit_remaining, wait))
            time.sleep(wait)


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
                _check_rate_limit(resp.headers)
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in _RETRYABLE_HTTP and attempt < attempts:
                # Prefer the server-specified Retry-After (seconds) over the
                # fixed backoff; fall back to the backoff schedule when the
                # header is absent or not a plain integer (some clients emit
                # an HTTP date instead).
                wait = None
                retry_after = e.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = int(retry_after)
                    except (TypeError, ValueError):
                        wait = None
                if wait is None:
                    wait = _BACKOFFS[min(attempt - 1, len(_BACKOFFS) - 1)]
                sys.stderr.write(
                    "WARN: HTTP %d on %s - sleeping %ds (attempt %d/%d)\n"
                    % (e.code, url, wait, attempt, attempts))
                time.sleep(wait)
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


# --- GraphQL bulk fetch ----------------------------------------------------
#
# The org has ~160 repos. Fetching trunk head, branch list, topics and
# description one REST call at a time means ~5 calls per repo (~430 total).
# GraphQL pulls all of that in a handful of paginated queries (one per 50
# repos), collapsing the bulk into ~4 queries. Pages / CI / README have no
# GraphQL equivalent and still go through the REST _request() path above.

_graphql_call_count = 0


def _graphql_query(query, variables=None, attempts=_MAX_ATTEMPTS):
    """POST ``query`` to the GitHub GraphQL API and return the ``data`` object.

    Retries with backoff on transient failures (HTTP 403/429 rate limit, or a
    network blip), mirroring ``_request``. On final failure returns ``{}`` so a
    single bad page degrades to "no more data" rather than crashing the run;
    every error is logged to stderr. Uses the same ``TOKEN`` global as REST.
    """
    global _graphql_call_count
    last_err = None
    for attempt in range(1, attempts + 1):
        _graphql_call_count += 1
        payload = json.dumps(
            {"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(
            GRAPHQL_API,
            data=payload,
            headers={
                "Authorization": "Bearer " + TOKEN,
                "Content-Type": "application/json",
                "Accept": "application/vnd.github+json",
                "User-Agent": USER_AGENT,
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            if not isinstance(data, dict):
                return {}
            if "errors" in data:
                sys.stderr.write("GraphQL errors: %s\n" % (data["errors"],))
            return data.get("data") or {}
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if e.code in _RETRYABLE_HTTP and attempt < attempts:
                delay = _BACKOFFS[min(attempt - 1, len(_BACKOFFS) - 1)]
                sys.stderr.write("GraphQL: HTTP %d - sleeping %ds "
                                 "(attempt %d/%d)\n"
                                 % (e.code, delay, attempt, attempts))
                time.sleep(delay)
                last_err = e
                continue
            sys.stderr.write("GraphQL HTTP %d: %s\n" % (e.code, body[:300]))
            return {}
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            if attempt < attempts:
                delay = _BACKOFFS[min(attempt - 1, len(_BACKOFFS) - 1)]
                sys.stderr.write("GraphQL: network error (%r) - sleeping %ds "
                                 "(attempt %d/%d)\n"
                                 % (e, delay, attempt, attempts))
                time.sleep(delay)
                continue
            sys.stderr.write("GraphQL: gave up after %d attempts: %r\n"
                             % (attempts, last_err))
            return {}
    return {}


def fetch_repos_graphql():
    """Fetch every org repo + its metadata in a few paginated GraphQL queries.

    Replaces ``list_repos()`` + per-repo ``fetch_trunk()`` + ``fetch_branches()``.
    Returns a list of dicts, one per repo, with keys:

      name, description, pushed_at, is_archived, visibility,
      default_branch, last_commit_date, last_commit_sha,
      branches (list of names, including the default branch),
      topics (list of names)

    ``default_branch`` falls back to ``"main"`` when the repo has no commits
    yet (``defaultBranchRef`` is null). Callers filter the default branch out
    of ``branches`` if they want non-default only.
    """
    all_repos = []
    cursor = None
    query = """
    query GetRepos($login: String!, $cursor: String) {
      rateLimit { cost remaining resetAt }
      organization(login: $login) {
        repositories(first: 50, after: $cursor,
                     orderBy: {field: NAME, direction: ASC}) {
          pageInfo { hasNextPage endCursor }
          nodes {
            name
            description
            pushedAt
            isArchived
            visibility
            defaultBranchRef {
              name
              target { ... on Commit { committedDate oid } }
            }
            refs(refPrefix: "refs/heads/", first: 30) { nodes { name } }
            repositoryTopics(first: 20) { nodes { topic { name } } }
          }
        }
      }
    }
    """
    while True:
        data = _graphql_query(query, {"login": ORG, "cursor": cursor})
        org_data = data.get("organization") or {}
        repos_data = org_data.get("repositories") or {}
        nodes = repos_data.get("nodes") or []
        for node in nodes:
            ref = node.get("defaultBranchRef") or {}
            target = ref.get("target") or {}
            all_repos.append({
                "name": node["name"],
                "description": node.get("description"),
                "pushed_at": node.get("pushedAt"),
                "is_archived": node.get("isArchived", False),
                "visibility": node.get("visibility", "PRIVATE"),
                "default_branch": ref.get("name") or "main",
                "last_commit_date": target.get("committedDate"),
                "last_commit_sha": target.get("oid"),
                "branches": [b["name"]
                             for b in (node.get("refs") or {}).get("nodes") or []],
                "topics": [t["topic"]["name"]
                           for t in (node.get("repositoryTopics") or {}).get("nodes") or []
                           if t.get("topic")],
            })
        if not nodes and cursor is None:
            # First page came back empty: org resolution or token-scope problem.
            sys.stderr.write("GraphQL: no repos returned for org=%r - check "
                             "GITHUB_TOKEN scope / org access\n" % ORG)
        page_info = repos_data.get("pageInfo") or {}
        if page_info.get("hasNextPage") and page_info.get("endCursor"):
            cursor = page_info["endCursor"]
        else:
            break
    sys.stderr.write("GraphQL: fetched %d repos (%d queries)\n"
                     % (len(all_repos), _graphql_call_count))
    return all_repos


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


def _cf_delete(url):
    """DELETE a Cloudflare API url. Returns a (body, status) tuple like
    ``_cf_get``: (parsed_json, 200) on success; (None, http_code) on an HTTP
    error; (None, -1) on any other failure. Never raises. The response body is
    best-effort (an empty 204/202 body yields ``None``)."""
    req = urllib.request.Request(url, method="DELETE", headers={
        "Authorization": "Bearer " + CF_TOKEN,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            try:
                body = json.load(resp)
            except Exception:
                body = None
            return body, resp.status
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


# --- Web Components rendering ---------------------------------------------

def _write_file(path, content):
    """Write ``content`` to ``path``, creating parent directories as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _render_data_js(data, titles, meta):
    """Render the ``data.js`` module with the actual catalog data."""
    return (
        "// Auto-generated by catalog.py \u2014 do not edit\n"
        "export const META = %s;\n"
        "export const DATA = %s;\n"
        "export const TITLES = %s;\n"
        % (_safe_json(meta), _safe_json(data), _safe_json(titles))
    )


def _render_card_js():
    """Return the static ``card.js`` Web Component source."""
    return r"""// card.js — <repo-card> Web Component (zero dependencies)
import { TITLES } from './data.js';

class RepoCard extends HTMLElement {
  set repo(data) {
    this._data = data;
    this.render();
  }
  get repo() { return this._data; }

  connectedCallback() {
    this._profile = document.body.dataset.profile || 'student';
  }

  render() {
    const d = this._data;
    if (!d) return;
    const icon = {lecture:'📚',notebook:'📓',sample:'💻',demo:'🎬'}[d.type] || '📦';
    const title = TITLES[d.name] || d.name.replace(/[-_]/g,' ').replace(/\b\w/g,c=>c.toUpperCase());

    if (this._profile === 'student') {
      this.innerHTML = `
        <div class="card-inner">
          <div class="card-icon">${icon}</div>
          <h3 class="card-title">${title}</h3>
          ${d.description ? `<p class="card-desc">${d.description}</p>` : ''}
          <div class="card-cta">
            ${d.develop_url ? `<a class="cta cta-primary" href="${d.develop_url}" target="_blank" rel="noopener">📖 Consulter le cours</a>` : ''}
            ${d.stable_url ? `<a class="cta cta-secondary" href="${d.stable_url}" target="_blank" rel="noopener">📊 Version stable</a>` : `<span class="cta cta-disabled" title="Pas encore disponible">📊 Version stable</span>`}
            ${!d.develop_url && !d.stable_url ? `<a class="cta cta-secondary" href="${d.github_url}" target="_blank" rel="noopener">💻 Voir sur GitHub</a>` : ''}
          </div>
        </div>`;
    } else {
      // Teacher: full detail
      const ciDot = d.ci_status === 'success' ? '🟢' : d.ci_status === 'failure' ? '🔴' : '⚪';
      const ciEmoji = d.ci_status === 'success' ? '✅' : d.ci_status === 'failure' ? '❌' : d.ci_status === 'in_progress' ? '🔄' : '';
      const topics = (d.topics||[]).map(t => {
        const cls = t.startsWith('area-') ? 'badge-area' : t.startsWith('status-') ? 'badge-status' : 'badge';
        return `<span class="badge ${cls}">${t}</span>`;
      }).join('');

      // CF branch links
      let cfLinks = '';
      if (d.cf_deployments && Object.keys(d.cf_deployments).length) {
        cfLinks = Object.entries(d.cf_deployments).map(([branch, dep]) => {
          const status = dep.status === 'complete' || dep.status === 'success' ? '✅' : dep.status === 'error' || dep.status === 'failure' ? '❌' : '⏳';
          return `<a class="branch-link" href="${dep.url}" target="_blank" rel="noopener">${status} ${branch}</a>`;
        }).join('');
      }

      // GitHub branches (not in CF)
      let ghBranches = '';
      if (d.branches && d.branches.length) {
        const cfBranches = new Set(Object.keys(d.cf_deployments || {}));
        const ghOnly = d.branches.filter(b => !cfBranches.has(b)).slice(0, 5);
        if (ghOnly.length) {
          ghBranches = ghOnly.map(b => `<span class="branch-chip">${b}</span>`).join('');
        }
      }

      const updated = d.updated_at ? `<div class="card-footer">Mis à jour: ${relativeTime(d.updated_at)}</div>` : '';

      this.innerHTML = `
        <div class="card-inner">
          <div class="card-header-row">
            <span class="status-dot">${ciDot}</span>
            <div class="card-icon">${icon}</div>
            <h3 class="card-title"><a href="${d.github_url}" target="_blank" rel="noopener">${d.name}</a></h3>
            ${d.ci_url ? `<a class="ci-link" href="${d.ci_url}" target="_blank" rel="noopener">${ciEmoji}</a>` : ''}
          </div>
          ${d.description ? `<p class="card-desc">${d.description}</p>` : ''}
          <div class="card-topics">${topics}</div>
          <div class="card-cta">
            ${d.develop_url ? `<a class="cta cta-primary" href="${d.develop_url}" target="_blank" rel="noopener">📖 Consulter le cours</a>` : ''}
            ${d.stable_url ? `<a class="cta cta-secondary" href="${d.stable_url}" target="_blank" rel="noopener">📊 Version stable</a>` : ''}
            ${!d.develop_url && !d.stable_url ? `<a class="cta cta-secondary" href="${d.github_url}" target="_blank" rel="noopener">💻 Voir sur GitHub</a>` : ''}
          </div>
          ${cfLinks ? `<div class="card-branches">${cfLinks}</div>` : ''}
          ${ghBranches ? `<div class="card-branches">${ghBranches}</div>` : ''}
          ${updated}
        </div>`;
    }
  }
}

function relativeTime(iso) {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return mins + ' min';
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return hrs + 'h';
  const days = Math.floor(hrs / 24);
  return days + 'j';
}

customElements.define('repo-card', RepoCard);
"""


def _render_app_js():
    """Return the static ``app.js`` bootstrap source."""
    return r"""// app.js — Application bootstrap (zero dependencies)
import { DATA, TITLES, META } from './data.js';
import './card.js';

const profile = document.body.dataset.profile || 'student';
const content = document.getElementById('content');
const searchInput = document.getElementById('search');
const filterChips = document.getElementById('filter-chips');
const statsBar = document.getElementById('stats-bar');
let currentFilter = 'all';
let searchQuery = '';

// Stats (teacher only)
if (profile === 'teacher' && statsBar) {
  const total = DATA.length;
  const green = DATA.filter(r => r.ci_status === 'success').length;
  const red = DATA.filter(r => r.ci_status === 'failure').length;
  statsBar.innerHTML = `
    <span class="stat"><strong>${total}</strong> ressources</span>
    <span class="stat stat-green"><strong>${green}</strong> ✅</span>
    <span class="stat stat-red"><strong>${red}</strong> ❌</span>
    <span class="stat">Généré le ${new Date(META.generated_at).toLocaleDateString('fr-FR')}</span>
  `;
}

// Filter chips
if (filterChips) {
  const chips = profile === 'teacher' 
    ? ['all','lecture','notebook','failed','recent']
    : ['all','lecture','notebook'];
  const labels = {all:'Tous',lecture:'📘 Cours',notebook:'📓 Notebooks',failed:'❌ Erreurs',recent:'🕐 Récents'};
  filterChips.innerHTML = chips.map(c => 
    `<button class="chip ${c==='all'?'active':''}" data-filter="${c}">${labels[c]}</button>`
  ).join('');
  filterChips.addEventListener('click', e => {
    const btn = e.target.closest('.chip');
    if (!btn) return;
    currentFilter = btn.dataset.filter;
    filterChips.querySelectorAll('.chip').forEach(b => b.classList.toggle('active', b === btn));
    render();
  });
}

// Search
if (searchInput) {
  searchInput.addEventListener('input', e => {
    searchQuery = e.target.value.toLowerCase();
    render();
  });
}

function getFiltered() {
  let items = [...DATA];
  
  // Student: only status-active
  if (profile === 'student') {
    items = items.filter(r => (r.topics||[]).includes('status-active'));
  }
  
  // Type filter
  if (currentFilter === 'lecture') items = items.filter(r => r.type === 'lecture');
  else if (currentFilter === 'notebook') items = items.filter(r => r.type === 'notebook' || r.type === 'sample' || r.type === 'demo');
  else if (currentFilter === 'failed') items = items.filter(r => r.ci_status === 'failure');
  else if (currentFilter === 'recent') {
    const dayAgo = Date.now() - 86400000;
    items = items.filter(r => r.updated_at && new Date(r.updated_at) > new Date(dayAgo));
  }
  
  // Search
  if (searchQuery) {
    items = items.filter(r => 
      r.name.toLowerCase().includes(searchQuery) ||
      (r.description||'').toLowerCase().includes(searchQuery) ||
      (r.topics||[]).some(t => t.toLowerCase().includes(searchQuery))
    );
  }
  
  // Sort
  if (profile === 'teacher') {
    const ciOrder = {failure:0, in_progress:1, queued:2, success:3};
    items.sort((a,b) => {
      const ca = ciOrder[a.ci_status] ?? 4;
      const cb = ciOrder[b.ci_status] ?? 4;
      if (ca !== cb) return ca - cb;
      return (b.updated_at||'').localeCompare(a.updated_at||'');
    });
  } else {
    items.sort((a,b) => a.type.localeCompare(b.type) || a.name.localeCompare(b.name));
  }
  
  return items;
}

function render() {
  const items = getFiltered();
  
  if (!items.length) {
    content.innerHTML = '<div class="empty">🔍 Aucun résultat trouvé</div>';
    return;
  }
  
  // Group by type for student view
  if (profile === 'student') {
    const lectures = items.filter(r => r.type === 'lecture');
    const others = items.filter(r => r.type !== 'lecture');
    let html = '';
    if (lectures.length) {
      html += '<h2 class="section-title">📚 Cours</h2><div class="card-grid">';
      html += lectures.map(r => cardHtml(r)).join('');
      html += '</div>';
    }
    if (others.length) {
      html += '<h2 class="section-title">📓 Notebooks &amp; Pratiques</h2><div class="card-grid">';
      html += others.map(r => cardHtml(r)).join('');
      html += '</div>';
    }
    content.innerHTML = html;
  } else {
    content.innerHTML = '<div class="card-grid">' + items.map(r => cardHtml(r)).join('') + '</div>';
  }
  
  // Upgrade to web components
  content.querySelectorAll('repo-card').forEach(el => {
    const name = el.dataset.name;
    const repo = DATA.find(r => r.name === name);
    if (repo) el.repo = repo;
  });
}

function cardHtml(r) {
  return `<repo-card data-name="${r.name}"></repo-card>`;
}

// Initial render
render();
"""


def _render_css():
    """Return the static ``styles.css`` source."""
    return """/* styles.css — ebpro teaching catalog (shared) */
:root {
  --bg: #f8fafc; --surface: #ffffff; --text: #1e293b; --text-muted: #64748b;
  --primary: #3b82f6; --primary-hover: #2563eb; --success: #22c55e;
  --error: #ef4444; --warning: #f59e0b; --border: #e2e8f0;
  --radius: 12px; --shadow: 0 1px 3px rgba(0,0,0,0.08);
  --shadow-md: 0 4px 12px rgba(0,0,0,0.12); --max-w: 1200px;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#0f172a; --surface:#1e293b; --text:#f1f5f9; --text-muted:#94a3b8; --border:#334155; --shadow:0 1px 3px rgba(0,0,0,0.3); --shadow-md:0 4px 12px rgba(0,0,0,0.4); }
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font: 16px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Inter,sans-serif; background: var(--bg); color: var(--text); }
a { color: var(--primary); text-decoration: none; }
a:hover { text-decoration: underline; }

/* Landing page */
.landing { min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2rem; padding: 2rem; text-align: center; }
.landing h1 { font-size: 2.5rem; }
.landing p { color: var(--text-muted); max-width: 500px; }
.landing-buttons { display: flex; gap: 1.5rem; flex-wrap: wrap; justify-content: center; }
.landing-btn { display: flex; flex-direction: column; align-items: center; gap: 0.5rem; padding: 2rem 3rem; border-radius: var(--radius); background: var(--surface); box-shadow: var(--shadow); transition: transform .2s, box-shadow .2s; font-size: 1.1rem; font-weight: 600; }
.landing-btn:hover { transform: translateY(-4px); box-shadow: var(--shadow-md); text-decoration: none; }
.landing-btn .icon { font-size: 3rem; }
.landing-stats { color: var(--text-muted); font-size: 0.9rem; }

/* Header (profile pages) */
header { position: sticky; top: 0; z-index: 100; background: var(--surface); border-bottom: 1px solid var(--border); padding: 1rem 2rem; display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; }
header h1 { font-size: 1.3rem; white-space: nowrap; }
header h1 span { opacity: 0.6; font-weight: 400; }
.search { flex: 1; min-width: 200px; max-width: 400px; padding: 0.5rem 1rem; border: 1px solid var(--border); border-radius: 8px; background: var(--bg); color: var(--text); font-size: 0.95rem; }
.search:focus { outline: none; border-color: var(--primary); }
.back-link { font-size: 0.9rem; color: var(--text-muted); }

/* Stats bar (teacher) */
.stats-bar { display: flex; gap: 1.5rem; padding: 0.75rem 2rem; background: var(--surface); border-bottom: 1px solid var(--border); font-size: 0.9rem; color: var(--text-muted); flex-wrap: wrap; }
.stat strong { color: var(--text); }
.stat-green strong { color: var(--success); }
.stat-red strong { color: var(--error); }

/* Filter chips */
.filter-chips { display: flex; gap: 0.5rem; padding: 1rem 2rem; flex-wrap: wrap; }
.chip { padding: 0.4rem 1rem; border-radius: 20px; border: 1px solid var(--border); background: var(--surface); color: var(--text); cursor: pointer; font-size: 0.85rem; transition: all .15s; }
.chip:hover { border-color: var(--primary); }
.chip.active { background: var(--primary); color: white; border-color: var(--primary); }

/* Content */
main { max-width: var(--max-w); margin: 0 auto; padding: 1rem 2rem 4rem; }
.section-title { font-size: 1.4rem; margin: 2rem 0 1rem; }
.card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 1.5rem; margin-bottom: 2rem; }
.empty { text-align: center; padding: 4rem; color: var(--text-muted); font-size: 1.2rem; }

/* Card (web component) */
repo-card { display: block; background: var(--surface); border-radius: var(--radius); box-shadow: var(--shadow); transition: box-shadow .2s; overflow: hidden; }
repo-card:hover { box-shadow: var(--shadow-md); }
.card-inner { padding: 1.5rem; }
.card-header-row { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; }
.status-dot { font-size: 0.8rem; }
.card-icon { font-size: 2rem; }
.card-title { font-size: 1.1rem; font-weight: 600; margin: 0.5rem 0; }
.card-title a { color: var(--text); }
.card-desc { color: var(--text-muted); font-size: 0.9rem; margin-bottom: 1rem; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.card-topics { display: flex; flex-wrap: wrap; gap: 0.3rem; margin-bottom: 1rem; }
.badge { padding: 0.15rem 0.5rem; border-radius: 4px; font-size: 0.75rem; background: var(--bg); border: 1px solid var(--border); }
.badge-area { background: #dbeafe; color: #1e40af; border-color: #93c5fd; }
.badge-status { background: #dcfce7; color: #166534; border-color: #86efac; }

/* CTA buttons */
.card-cta { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 1rem; }
.cta { padding: 0.5rem 1rem; border-radius: 8px; font-size: 0.85rem; font-weight: 500; text-align: center; transition: all .15s; }
.cta-primary { background: var(--primary); color: white; }
.cta-primary:hover { background: var(--primary-hover); text-decoration: none; }
.cta-secondary { background: var(--bg); color: var(--text); border: 1px solid var(--border); }
.cta-secondary:hover { border-color: var(--primary); text-decoration: none; }
.cta-disabled { background: var(--bg); color: var(--text-muted); border: 1px solid var(--border); opacity: 0.5; cursor: not-allowed; }

/* Branches (teacher) */
.card-branches { display: flex; flex-wrap: wrap; gap: 0.3rem; margin-top: 0.75rem; }
.branch-link { padding: 0.2rem 0.5rem; border-radius: 4px; font-size: 0.75rem; background: var(--bg); border: 1px solid var(--border); color: var(--text); }
.branch-link:hover { border-color: var(--primary); text-decoration: none; }
.branch-chip { padding: 0.2rem 0.5rem; border-radius: 4px; font-size: 0.75rem; background: var(--bg); color: var(--text-muted); }
.card-footer { margin-top: 0.75rem; font-size: 0.8rem; color: var(--text-muted); }
.ci-link { margin-left: auto; font-size: 1rem; }

/* Student: hide teacher-only elements (safety net) */
[data-profile="student"] .card-topics,
[data-profile="student"] .card-branches,
[data-profile="student"] .card-footer,
[data-profile="student"] .stats-bar,
[data-profile="student"] .ci-link { display: none; }

/* Responsive */
@media (max-width: 600px) {
  .card-grid { grid-template-columns: 1fr; }
  header { padding: 0.75rem 1rem; }
  main { padding: 1rem; }
  .landing-buttons { flex-direction: column; }
  .landing-btn { width: 100%; max-width: 300px; }
}
"""


def _render_landing():
    """Return the ``index.html`` landing page source."""
    return """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ebpro — Plateforme d’enseignement</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <div class="landing">
    <h1>📚 ebpro</h1>
    <p>Plateforme d’enseignement — apprentissage par la pratique. Notebooks, cours et laboratoires interactifs avec intégration continue.</p>
    <div class="landing-buttons">
      <a class="landing-btn" href="/etudiant"><span class="icon">🎓</span>Espace Étudiant</a>
      <a class="landing-btn" href="/enseignant"><span class="icon">👨‍🏫</span>Espace Enseignant</a>
    </div>
    <div class="landing-stats" id="landing-stats"></div>
  </div>
  <script type="module">
    import { META } from './data.js';
    document.getElementById('landing-stats').textContent = 
      `${META.count} ressources · Généré le ${new Date(META.generated_at).toLocaleDateString('fr-FR')}`;
  </script>
</body>
</html>
"""


def _render_profile_page(profile):
    """Return the profile page HTML (``etudiant.html`` or ``enseignant.html``)."""
    if profile == "student":
        return """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ebpro — Espace Étudiant</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body data-profile="student">
  <header>
    <a class="back-link" href="/">←</a>
    <h1>🎓 ebpro <span>— Espace Étudiant</span></h1>
    <input type="search" id="search" class="search" placeholder="Rechercher un cours...">
  </header>
  <nav id="filter-chips" class="filter-chips"></nav>
  <main id="content"><div class="empty">⏳ Chargement...</div></main>
  <script type="module" src="app.js"></script>
</body>
</html>
"""
    else:
        return """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ebpro — Espace Enseignant</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body data-profile="teacher">
  <header>
    <a class="back-link" href="/">←</a>
    <h1>👨‍🏫 ebpro <span>— Espace Enseignant</span></h1>
    <input type="search" id="search" class="search" placeholder="Rechercher...">
  </header>
  <div id="stats-bar" class="stats-bar"></div>
  <nav id="filter-chips" class="filter-chips"></nav>
  <main id="content"><div class="empty">⏳ Chargement...</div></main>
  <script type="module" src="app.js"></script>
</body>
</html>
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
            "cf_develop_url": develop_url,
            "cf_deployments": cf_deps,
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

def _render_all(manifest):
    """Write all output files for the two-page Web Components catalog."""
    data = _build_data(manifest)["repos"]
    titles = _build_titles(manifest)
    meta = {
        "generated_at": manifest["generated_at"],
        "org": manifest["org"],
        "count": manifest["count"],
    }

    os.makedirs(OUT_DIR, exist_ok=True)

    _write_file(os.path.join(OUT_DIR, "data.js"), _render_data_js(data, titles, meta))
    _write_file(os.path.join(OUT_DIR, "card.js"), _render_card_js())
    _write_file(os.path.join(OUT_DIR, "app.js"), _render_app_js())
    _write_file(os.path.join(OUT_DIR, "styles.css"), _render_css())
    _write_file(os.path.join(OUT_DIR, "index.html"), _render_landing())
    _write_file(os.path.join(OUT_DIR, "etudiant.html"), _render_profile_page("student"))
    _write_file(os.path.join(OUT_DIR, "enseignant.html"), _render_profile_page("teacher"))


# --- entry point ----------------------------------------------------------

def main():
    global TOKEN, CF_TOKEN
    TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
    if not TOKEN:
        sys.stderr.write("ERROR: GITHUB_TOKEN environment variable is empty\n")
        return 1
    CF_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()

    all_repos = fetch_repos_graphql()
    if not all_repos:
        # A 0-repo result means the bulk GraphQL fetch failed outright (bad
        # token scope / org resolution). Aborting here (non-zero exit) stops
        # the deploy step from publishing an empty catalog over production.
        sys.stderr.write(
            "FATAL: GraphQL returned 0 repos - aborting to avoid deploying "
            "an empty catalog (check GITHUB_TOKEN scope / org access)\n")
        return 1
    kept = [r for r in all_repos
            if r["name"].startswith(PREFIXES) and not r["is_archived"]]
    kept.sort(key=lambda r: r["name"])

    data = {}
    loop_start = time.time()
    for r in kept:
        name = r["name"]
        sys.stderr.write("Processing %s ...\n" % name)
        # CF Pages deployments lookup: {} locally (no CF_API_TOKEN); in CI it
        # maps branch -> {url, environment, status, sha, created_on, ...}.
        cf_deps = cf_deployments_for(name) if CF_TOKEN else {}
        db = r["default_branch"]
        # Trunk head, branch list and topics come from the GraphQL bulk fetch
        # (fetch_repos_graphql); only Pages / CI / README still hit REST.
        trunk_date = r.get("last_commit_date")
        trunk = ({"branch": db, "sha": r.get("last_commit_sha"),
                  "date": trunk_date, "subject": None}
                 if trunk_date else None)
        branches = sorted(b for b in (r.get("branches") or []) if b != db)
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
        # Inter-repo pacing: a small delay between repos keeps our request
        # burst low enough to stay under GitHub's secondary (abuse) rate
        # limits -- the ones that triggered the fixed-30s 403 sleeps.
        time.sleep(0.1)
    elapsed = time.time() - loop_start
    print("Total: %d repos in %.1fs | API calls: %d | "
          "Rate limit remaining: %d"
          % (len(data), elapsed, _call_count, _rate_limit_remaining))

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

    _render_all(manifest)

    readme_missing = sum(1 for r in data.values()
                         if not r.get("readme_title"))
    sys.stderr.write(
        "Wrote %s/ (2-page Web Components catalog, %d repos)\n"
        % (OUT_DIR, len(data)))
    print("Total GitHub API requests: %d" % _call_count)
    print("Total GraphQL queries: %d" % _graphql_call_count)
    if CF_TOKEN:
        print("Total Cloudflare API requests: %d" % _cf_call_count)
        print("CF account id: %s" % (cf_account_id() or "NONE"))
    else:
        print("CF account id: (no CF token -> no preview lookups)")
    print("Repos without README title (404/no-heading/skipped): %d"
          % readme_missing)
    return 0


# --- CF Pages deployment cleanup ------------------------------------------

def cmd_cleanup(args):
    """Prune old Cloudflare Pages preview deployments.

    Policy: keep the last ``N`` deployments per branch (default 3); the
    production branch (``develop``) is never touched. Supports ``--dry-run``
    (report only, no deletions) and ``--keep N``.
    """
    global CF_TOKEN
    CF_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()
    if not CF_TOKEN:
        sys.stderr.write("ERROR: CF_API_TOKEN environment variable is empty\n")
        return 1

    account_id = cf_account_id()
    if not account_id:
        sys.stderr.write("ERROR: Could not resolve CF account ID\n")
        return 1

    keep = args.keep
    dry_run = args.dry_run
    print("Account: %s" % account_id)
    print("Retention: keep last %d per branch" % keep)
    print("Mode: %s" % ("DRY RUN" if dry_run else "LIVE"))
    print()

    # List all Pages projects in the account (paginate).
    projects = []
    cursor = None
    while True:
        url = "%s/accounts/%s/pages/projects?per_page=50" % (CF_API, account_id)
        if cursor:
            url += "&cursor=" + cursor
        data, status = _cf_get(url)
        if not (isinstance(data, dict) and data.get("success")
                and isinstance(data.get("result"), list)):
            if data is None:
                sys.stderr.write("CF: /pages/projects -> HTTP %s\n" % status)
            break
        projects.extend(data["result"])
        info = data.get("result_info") or {}
        if not info.get("has_more"):
            break
        cursor = info.get("cursor")
        if not cursor:
            break

    print("Found %d Pages projects" % len(projects))
    print()

    total_deleted = 0
    total_kept = 0

    for project in projects:
        proj_name = project.get("name", "")
        if not proj_name:
            continue

        # All deployments for this project (paginate). NOTE: this endpoint
        # rejects per_page > 20 (HTTP 400 / error 8000024), so 20 is the max.
        deployments = []
        cursor = None
        while True:
            url = "%s/accounts/%s/pages/projects/%s/deployments?per_page=20" % (
                CF_API, account_id, proj_name)
            if cursor:
                url += "&cursor=" + cursor
            data, status = _cf_get(url)
            if not (isinstance(data, dict) and data.get("success")
                    and isinstance(data.get("result"), list)):
                if data is None:
                    sys.stderr.write(
                        "CF: /pages/projects/%s/deployments -> HTTP %s\n"
                        % (proj_name, status))
                break
            deployments.extend(data["result"])
            info = data.get("result_info") or {}
            if not info.get("has_more"):
                break
            cursor = info.get("cursor")
            if not cursor:
                break

        if not deployments:
            continue

        # Group by branch, newest first (created_on desc).
        by_branch = {}
        for dep in deployments:
            by_branch.setdefault(dep.get("branch", "unknown"), []).append(dep)
        for deps in by_branch.values():
            deps.sort(key=lambda d: d.get("created_on", "") or "", reverse=True)

        proj_deleted = 0
        for branch, deps in by_branch.items():
            # Never touch the production branch.
            if branch == "develop":
                total_kept += len(deps)
                continue

            to_keep = deps[:keep]
            to_delete = deps[keep:]
            total_kept += len(to_keep)
            for dep in to_delete:
                dep_id = dep.get("id", "")
                dep_url = dep.get("url", "")
                created = (dep.get("created_on", "") or "")[:10]
                if dry_run:
                    print("  [DRY] Would delete: %s / %s / %s (%s) %s"
                          % (proj_name, branch, dep_id, created, dep_url))
                    total_deleted += 1
                    continue
                del_url = "%s/accounts/%s/pages/projects/%s/deployments/%s" % (
                    CF_API, account_id, proj_name, dep_id)
                _body, status = _cf_delete(del_url)
                if status in (200, 204):
                    print("  [DEL] %s / %s / %s (%s)"
                          % (proj_name, branch, dep_id, created))
                    proj_deleted += 1
                elif status == 404:
                    print("  [404] %s / %s / %s (already gone)"
                          % (proj_name, branch, dep_id))
                else:
                    print("  [ERR] %s / %s / %s: HTTP %s"
                          % (proj_name, branch, dep_id, status))
            total_deleted += proj_deleted

        if proj_deleted > 0:
            print("  %s: deleted %d" % (proj_name, proj_deleted))

    print()
    suffix = " (dry run)" if dry_run else ""
    print("Summary: %d kept, %d deleted%s" % (total_kept, total_deleted, suffix))
    return 0


if __name__ == "__main__":
    try:
        argv = sys.argv[1:]
        if not argv or argv[0] != "cleanup":
            # No subcommand -> build the catalog (original behaviour).
            sys.exit(main())
        elif argv[0] == "cleanup":
            parser_cleanup = argparse.ArgumentParser(
                prog="catalog.py cleanup",
                description="Prune old Cloudflare Pages preview deployments.")
            parser_cleanup.add_argument(
                "--keep", type=int, default=3,
                help="Deployments to keep per branch (default: 3)")
            parser_cleanup.add_argument(
                "--dry-run", action="store_true",
                help="Print deletions without executing")
            args_cleanup = parser_cleanup.parse_args(argv[1:])
            sys.exit(cmd_cleanup(args_cleanup))
    except Exception as e:
        sys.stderr.write("FATAL: %s\n" % e)
        sys.exit(1)
