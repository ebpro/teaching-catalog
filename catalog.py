#!/usr/bin/env python3
"""teaching-catalog generator.

Scans the ``ebpro`` GitHub org (``GET /orgs/ebpro/repos``) and renders a
self-contained static card grid of the teaching surface under ``./public/``:

  * one pastel card per repo tagged area-lecture / area-sample /
    area-standard / area-reference / area-student (plus anything tagged
    ``fmt-ipynb`` that carries no teaching area),
  * archived repos and the non-teaching areas (infra / cicd / image /
    meta / personal) are excluded,
  * card content: repo title, 2-line clamped description, one area chip +
    one status chip, and a footer with Site (render-callers only), Source
    (GitHub) and the relative last-update date.

The published-site links are generated per area WITHOUT probing the URLs at
build time (too slow / flaky for a cron job); a stale link is acceptable.

Tailwind CSS is delivered via the Play CDN in the template (see the note
there); fonts are the system stack, no images or icons.

Pure Python 3 stdlib. Auth: reads ``GITHUB_TOKEN`` (required) from the
environment. The ``cleanup`` subcommand (Cloudflare Pages preview pruning,
kept for ``.github/workflows/cleanup.yml``) additionally reads
``CF_API_TOKEN`` and is unaffected by the catalog rendering path above.
"""

import argparse
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
API = "https://api.github.com"
OUT_DIR = "public"
USER_AGENT = "ebpro-teaching-catalog"

# Base under which the rendered course sites are published (deploy-static job
# of the shared Quarto render pipeline). No build-time probing (see docstring).
SITE_BASE = "https://bruno.univ-tln.fr/teaching-material"

# --- teaching surface (classification topics, ebpro org labeling) ----------

INCLUDE_AREAS = ("lecture", "sample", "standard", "reference", "student")
EXCLUDE_AREAS = ("infra", "cicd", "image", "meta", "personal")
# Areas whose repos are Quarto render-callers -> get a "Site" link.
RENDER_CALLER_AREAS = ("lecture", "sample", "standard")
# Pseudo-area for fmt-ipynb repos without a teaching area tag.
IPYNB_AREA = "ipynb"

AREA_LABELS = {
    "lecture": "Lecture",
    "sample": "Samples",
    "standard": "Standards",
    "reference": "Références",
    "student": "Étudiants",
    IPYNB_AREA: "Notebooks",
}

# Pastel card backgrounds + borders (Tailwind classes).
AREA_CARD = {
    "lecture": "bg-sky-100 border-sky-300",
    "sample": "bg-emerald-100 border-emerald-300",
    "standard": "bg-amber-100 border-amber-300",
    "reference": "bg-violet-100 border-violet-300",
    "student": "bg-rose-100 border-rose-300",
    IPYNB_AREA: "bg-orange-100 border-orange-300",
}

# Area chip styles (one shade darker than the card).
AREA_CHIP = {
    "lecture": "bg-sky-200 text-sky-900",
    "sample": "bg-emerald-200 text-emerald-900",
    "standard": "bg-amber-200 text-amber-900",
    "reference": "bg-violet-200 text-violet-900",
    "student": "bg-rose-200 text-rose-900",
    IPYNB_AREA: "bg-orange-200 text-orange-900",
}

# Statuses rendered with reduced emphasis.
STATUS_MUTED = ("frozen", "duplicate")

STATUS_LABELS = {
    "active": "actif",
    "partial": "partiel",
    "stub": "ébauche",
    "legacy": "héritage",
    "frozen": "figé",
    "duplicate": "dupliqué",
}


# --- API layer ------------------------------------------------------------

_call_count = 0
TOKEN = ""
_MAX_ATTEMPTS = 4
_BACKOFFS = (30, 60, 120)
_RETRYABLE_HTTP = (403, 429)

# --- per-call pacing -------------------------------------------------------
# Spacing every HTTP request (REST + GraphQL) by _API_CALL_INTERVAL keeps the
# burst rate low enough to stay under GitHub's secondary (abuse) rate limits,
# eliminating the fixed-30s 403 sleeps that previously triggered on bursts.
_last_api_call = 0.0
_API_CALL_INTERVAL = 0.2  # 200 ms between calls


def _pace():
    """Ensure minimum interval between API calls."""
    global _last_api_call
    elapsed = time.monotonic() - _last_api_call
    if elapsed < _API_CALL_INTERVAL:
        time.sleep(_API_CALL_INTERVAL - elapsed)
    _last_api_call = time.monotonic()

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
    _pace()
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



# --- Cloudflare Pages API layer (best-effort; never raises) ---------------
#
# Used only when CF_API_TOKEN is set (i.e. in CI). Locally the token is empty
# and no CF calls are made (branch rows fall back to GitHub links). Every CF
# failure is swallowed -> cf_deployments = {} so a Cloudflare problem can
# never break the (GitHub-backed) catalog.
#
# Inverted approach: list all CF Pages projects ONCE (1-2 paginated calls),
# then fetch deployments only for the projects that match our catalog repos.
# Total CF API calls: ~26 instead of 87 (56 of which were 404s).

CF_TOKEN = ""
CF_API = "https://api.cloudflare.com/client/v4"
_cf_call_count = 0
_cf_account_id = None
_cf_account_fetched = False
_cf_accounts_status = None
_cf_projects_listed = 0
_cf_deployments_fetched = 0
_cf_other_logged = False


def _cf_get(url):
    """GET a Cloudflare API url. Returns a (body, status) tuple:
    (parsed_json, 200) on success; (None, http_code) on an HTTP error;
    (None, -1) on any other failure (network, timeout, ...). Never raises.

    Transient failures (non-404 HTTP errors, network/timeout errors) are
    retried once after a short pause. 404 is definitive (no retry)."""
    global _cf_call_count, _cf_other_logged
    last_status = -1
    for attempt in range(2):
        _pace()
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
            last_status = e.code
            if not _cf_other_logged:
                _cf_other_logged = True
                try:
                    body = e.read().decode("utf-8", "replace")[:400]
                except Exception:
                    body = "<unreadable>"
                sys.stderr.write("CF: HTTP %s (first) on %s: %s\n"
                                 % (e.code, url.split("?")[0], body))
            if e.code == 404:
                return None, e.code  # Not found is definitive
        except Exception as e:
            last_status = -1
            if not _cf_other_logged:
                _cf_other_logged = True
                sys.stderr.write("CF: non-HTTP error (first) on %s: %r\n"
                                 % (url.split("?")[0], e))
        if attempt == 0:
            time.sleep(2)
    return None, last_status


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
    """Fetch the Cloudflare account id ONCE (cached). None on any failure.
    Prefers the CF_ACCOUNT_ID environment variable when set (saves 1 call)."""
    global _cf_account_id, _cf_account_fetched, _cf_accounts_status
    if _cf_account_fetched:
        return _cf_account_id
    _cf_account_fetched = True
    if not CF_TOKEN:
        return None  # no token -> no CF at all (guarantees zero CF calls)
    env_id = os.environ.get("CF_ACCOUNT_ID", "").strip()
    if env_id:
        _cf_account_id = env_id
        _cf_accounts_status = 0
        return _cf_account_id
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


def cf_list_all_projects():
    """List ALL Cloudflare Pages projects in the account (paginated).

    Returns ``{project_name: {"subdomain": str, "production_branch": str}}``.
    Uses 1-2 API calls (50 projects per page). On any failure returns ``{}``.
    """
    global _cf_projects_listed
    acct = cf_account_id()
    if not acct:
        return {}
    projects = {}
    cursor = None
    while True:
        url = "%s/accounts/%s/pages/projects?per_page=50" % (CF_API, acct)
        if cursor:
            url += "&cursor=%s" % cursor
        data, status = _cf_get(url)
        if not (isinstance(data, dict) and data.get("success")
                and isinstance(data.get("result"), list)):
            if status != 200:
                sys.stderr.write(
                    "CF: /pages/projects -> HTTP %s (stopping pagination)\n"
                    % status)
            break
        for p in data["result"]:
            name = p.get("name", "")
            if name:
                projects[name] = {
                    "subdomain": p.get("subdomain") or name,
                    "production_branch": p.get("production_branch") or "main",
                }
        info = data.get("result_info") or {}
        if info.get("cursor"):
            cursor = info["cursor"]
        else:
            break
    _cf_projects_listed = len(projects)
    sys.stderr.write("CF: listed %d projects\n" % len(projects))
    return projects


def cf_get_deployments(project_name, per_page=20):
    """Map of ``{branch: deployment}`` for a single CF Pages project.

    For each branch (both ``preview`` and ``production`` environments) keeps
    the most recent (max ``created_on``) deployment, capturing the fields
    needed to render the same info the CF Pages dashboard shows:

        url, branch, environment, status, sha, commit_message, created_on

    Uses 1 API call. On any failure returns ``{}``.
    """
    global _cf_deployments_fetched
    acct = cf_account_id()
    if not acct:
        return {}
    # per_page must be <= 20 for this endpoint: per_page=100 is rejected with
    # HTTP 400 / error 8000024. 20 is the documented default and is ample for
    # the latest deployment per branch.
    url = "%s/accounts/%s/pages/projects/%s/deployments?per_page=%d" % (
        CF_API, acct, project_name, per_page)
    data, status = _cf_get(url)
    if not (isinstance(data, dict) and data.get("success")
            and isinstance(data.get("result"), list)):
        if status != 200:
            sys.stderr.write(
                "CF: /pages/projects/%s/deployments -> HTTP %s\n"
                % (project_name, status))
        return {}
    _cf_deployments_fetched += 1
    latest = {}
    for d in data["result"]:
        branch = d.get("branch") or ""
        du = d.get("url")
        created = d.get("created_on", "")
        if not du:
            continue
        summary = d.get("summary") or {}
        git_info = summary.get("git_info") or {}
        env = d.get("environment") or ""
        dep = {
            "url": du,
            "branch": branch,
            "environment": env,
            # Top-level status ("uploaded"/"in_progress"/"complete"/"error");
            # fall back to the summary status if the top-level one is absent.
            "status": d.get("status") or summary.get("status"),
            "sha": git_info.get("sha") or "",
            "commit_message": git_info.get("commit_message") or "",
            "created_on": created,
        }
        key = branch or env or "production"
        cur = latest.get(key)
        if cur is None or created > cur["created_on"]:
            latest[key] = dep
    return latest


# --- REST bulk fetch --------------------------------------------------------


def fetch_repos():
    """Return every repo of the org via paginated
    ``GET /orgs/ebpro/repos?type=all&per_page=100`` (``all`` includes
    archived + private; archived ones are filtered out by _classify)."""
    repos = []
    page = 1
    while True:
        url = "%s/orgs/%s/repos?type=all&per_page=100&page=%d" % (
            API, ORG, page)
        batch = _request(url)
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return repos


# --- classification helpers --------------------------------------------------


def _classify(repo):
    """Return the display area key for ``repo``, or None to exclude it.

    Rules (design spec): archived -> excluded; any excluded area topic ->
    excluded; otherwise the first teaching area in INCLUDE_AREAS order;
    a repo with no teaching area but the ``fmt-ipynb`` topic falls back to
    the pseudo-area ``ipynb``.
    """
    if repo.get("archived"):
        return None
    topics = repo.get("topics") or []
    areas = {t[len("area-"):] for t in topics if t.startswith("area-")}
    if areas & set(EXCLUDE_AREAS):
        return None
    for area in INCLUDE_AREAS:
        if area in areas:
            return area
    if "fmt-ipynb" in topics:
        return IPYNB_AREA
    return None


def _status_of(repo):
    """First ``status-*`` topic suffix, or None."""
    for t in repo.get("topics") or []:
        if t.startswith("status-"):
            return t[len("status-"):]
    return None


def _relative_fr(iso):
    """``2026-10-11T08:12:00Z`` -> ``il y a 3 j`` (French, coarse)."""
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 3600:
        return "il y a %d min" % max(1, int(secs // 60))
    if secs < 86400:
        return "il y a %d h" % int(secs // 3600)
    days = secs / 86400.0
    if days < 31:
        return "il y a %d j" % int(days)
    if days < 365:
        return "il y a %d sem" % int(days // 7)
    if days < 730:
        return "il y a 1 an"
    return "il y a %d ans" % int(days // 365)


def _gen_date_fr(iso_utc):
    """``2026-10-14T10:00:00Z`` -> ``14/10/2026 à 12:00`` (local-free, UTC)."""
    try:
        dt = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return iso_utc
    return dt.strftime("%d/%m/%Y à %H:%M UTC")


# --- rendering ---------------------------------------------------------------


def _render_card(c):
    """Return the <article> for one catalog entry ``c``."""
    area = c["area"]
    muted = " opacity-60" if c["status"] in STATUS_MUTED else ""
    esc_name = html.escape(c["name"])
    esc_desc = html.escape(c["description"] or "")
    parts = [
        '<article data-area="%s" class="card %s border rounded-xl p-4 '
        'flex flex-col%s">' % (area, AREA_CARD[area], muted),
        '<h3 class="font-semibold text-slate-900 leading-snug break-words">'
        '%s</h3>' % esc_name,
        '<p class="mt-1 text-sm text-slate-600 line-clamp-2">%s</p>'
        % (esc_desc or "&nbsp;"),
        '<div class="mt-3 flex flex-wrap items-center gap-2 text-[11px] '
        'font-medium">',
        '<span class="px-2 py-0.5 rounded-full %s">%s</span>'
        % (AREA_CHIP[area], AREA_LABELS[area]),
    ]
    if c["status"]:
        label = STATUS_LABELS.get(c["status"], html.escape(c["status"]))
        parts.append(
            '<span class="px-2 py-0.5 rounded-full bg-white/70 '
            'text-slate-700">%s</span>' % label)
    if c["ipynb"]:
        parts.append(
            '<span class="w-2 h-2 rounded-full bg-orange-300" '
            'title="Contenu Jupyter (ipynb)"></span>')
    parts.append("</div>")
    links = []
    if c["site_url"]:
        links.append('<a class="underline-offset-2 hover:underline '
                     'text-slate-800" href="%s">Site</a>' % c["site_url"])
    links.append('<a class="underline-offset-2 hover:underline '
                 'text-slate-800" href="%s">Source</a>' % c["github_url"])
    updated = ("Mis à jour %s" % c["updated"]) if c["updated"] else ""
    parts.append(
        '<footer class="mt-auto pt-3 text-xs flex items-center gap-3 '
        'text-slate-600">%s%s</footer>'
        % ("".join(links),
           ('<span class="ml-auto whitespace-nowrap">%s</span>' % updated)
           if updated else ""))
    parts.append("</article>")
    return "\n      ".join(parts)


def _render_index(cards, generated_at):
    """Return the full index.html source (Tailwind Play CDN delivery)."""
    count = len(cards)
    # Filter chips: "Tous" + each area present, in INCLUDE_AREAS order.
    order = list(INCLUDE_AREAS) + [IPYNB_AREA]
    present = [a for a in order if any(c["area"] == a for c in cards)]
    tally = {a: sum(1 for c in cards if c["area"] == a) for a in present}
    chip_btns = [
        '<button data-filter="all" class="px-3 py-1 rounded-full text-sm '
        'bg-slate-800 text-white ring-2 ring-slate-800">Tous (%d)</button>'
        % count]
    for a in present:
        chip_btns.append(
            '<button data-filter="%s" class="px-3 py-1 rounded-full text-sm '
            'bg-white border border-slate-300 text-slate-700 '
            'hover:bg-slate-100">%s (%d)</button>'
            % (a, AREA_LABELS[a], tally[a]))
    grid = "\n      ".join(_render_card(c) for c in cards)
    return """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Catalogue ebpro — Supports pédagogiques</title>
  <!-- Tailwind Play CDN (v1 pragmatique : page statique régénérée par cron).
       Swap to compiled CSS when a build step lands. -->
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-50 text-slate-800 antialiased">
  <header class="max-w-7xl mx-auto px-4 pt-10 pb-4">
    <h1 class="text-3xl font-semibold tracking-tight text-slate-900">
      Catalogue ebpro</h1>
    <p class="mt-1 text-slate-500">Supports pédagogiques de l'organisation
      ebpro — cours, samples et références.</p>
    <p class="mt-1 text-sm text-slate-400">%d supports</p>
  </header>
  <nav aria-label="Filtres" id="filters"
       class="max-w-7xl mx-auto px-4 pb-2 flex flex-wrap items-center gap-2">
    <span class="text-xs uppercase tracking-wide text-slate-400 mr-1"
          >Filtres</span>
    %s
  </nav>
  <main class="max-w-7xl mx-auto px-4 py-6">
    <div id="grid"
         class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
      %s
    </div>
  </main>
  <footer class="max-w-7xl mx-auto px-4 pb-10 text-xs text-slate-400">
    Généré le %s · %d dépôts · source :
    <a class="underline" href="https://github.com/ebpro/teaching-catalog"
       >teaching-catalog</a>
  </footer>
  <script>
    // Filtres par aire, bascule client-side sans dépendance.
    const buttons = document.querySelectorAll('#filters button');
    const cards = document.querySelectorAll('#grid .card');
    buttons.forEach(btn => btn.addEventListener('click', () => {
      const f = btn.dataset.filter;
      buttons.forEach(b => {
        const on = b === btn;
        b.classList.toggle('ring-2', on);
        b.classList.toggle('bg-slate-800', on);
        b.classList.toggle('text-white', on);
      });
      cards.forEach(c =>
        c.classList.toggle('hidden', f !== 'all' && c.dataset.area !== f));
    }));
  </script>
</body>
</html>
""" % (count, "\n    ".join(chip_btns), grid,
       _gen_date_fr(generated_at), count)


def _write_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# --- entry point ----------------------------------------------------------

def main():
    global TOKEN
    start_ts = time.time()
    TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
    if not TOKEN:
        sys.stderr.write("ERROR: GITHUB_TOKEN environment variable is empty\n")
        return 1

    all_repos = fetch_repos()
    if not all_repos:
        # A 0-repo result means the fetch failed outright (bad token scope /
        # org resolution). Aborting here (non-zero exit) stops the deploy
        # step from publishing an empty catalog over production.
        sys.stderr.write(
            "FATAL: /orgs/%s/repos returned 0 repos - aborting to avoid "
            "deploying an empty catalog (check GITHUB_TOKEN scope / org "
            "access)\n" % ORG)
        return 1

    cards = []
    for r in all_repos:
        area = _classify(r)
        if area is None:
            continue
        site_url = (
            "%s/%s/develop/" % (SITE_BASE, r["name"])
            if area in RENDER_CALLER_AREAS else None)
        cards.append({
            "name": r["name"],
            "description": r.get("description"),
            "topics": r.get("topics") or [],
            "area": area,
            "status": _status_of(r),
            "ipynb": "fmt-ipynb" in (r.get("topics") or []),
            "pushed_at": r.get("pushed_at"),
            "updated": _relative_fr(r.get("pushed_at")),
            "site_url": site_url,
            "github_url": r.get("html_url")
                          or "https://github.com/%s/%s" % (ORG, r["name"]),
        })

    # Group by area (teaching order first), then alphabetical within an area.
    order = {a: i for i, a in enumerate(INCLUDE_AREAS + (IPYNB_AREA,))}
    cards.sort(key=lambda c: (order[c["area"]], c["name"]))

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    os.makedirs(OUT_DIR, exist_ok=True)
    _write_file(os.path.join(OUT_DIR, "index.html"),
                _render_index(cards, generated_at))
    with open(os.path.join(OUT_DIR, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump({
            "generated_at": generated_at,
            "org": ORG,
            "count": len(cards),
            "repos": cards,
        }, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print("Total: %d catalog cards from %d org repos in %.1fs | "
          "API calls: %d" % (len(cards), len(all_repos),
                             time.time() - start_ts, _call_count))
    sys.stderr.write("Wrote %s/ (Tailwind card grid, %d repos)\n"
                     % (OUT_DIR, len(cards)))
    return 0


# --- CF Pages deployment cleanup ------------------------------------------

def cmd_cleanup(args):
    """Prune old Cloudflare Pages preview deployments.

    Policy: keep the last ``N`` deployments per branch (default 2); the
    production branch of each project (from the CF API, falling back to
    ``develop``) is never touched, and the catalog's own ``teaching-catalog``
    project is always skipped. Supports ``--dry-run`` (report only, no
    deletions) and ``--keep N``.
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
        if proj_name == "teaching-catalog":
            continue  # Never clean up the catalog's own deployments

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

        # Production branch for this project (from CF project metadata);
        # fall back to "develop" to preserve the previous behaviour.
        prod_branch = project.get("production_branch") or "develop"

        proj_deleted = 0
        for branch, deps in by_branch.items():
            # Never touch the production branch.
            if branch == prod_branch:
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
                "--keep", type=int, default=2,
                help="Deployments to keep per branch (default: 2)")
            parser_cleanup.add_argument(
                "--dry-run", action="store_true",
                help="Print deletions without executing")
            args_cleanup = parser_cleanup.parse_args(argv[1:])
            sys.exit(cmd_cleanup(args_cleanup))
    except Exception as e:
        sys.stderr.write("FATAL: %s\n" % e)
        sys.exit(1)
