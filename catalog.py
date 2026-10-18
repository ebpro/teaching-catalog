#!/usr/bin/env python3
"""teaching-catalog generator.

Scans the ``ebpro`` GitHub org (``GET /orgs/ebpro/repos``) and renders a
self-contained static card grid of the teaching surface under ``./public/``:

  * three display categories assigned by repo NAME PREFIX (spec v3):
    ``lecture-`` -> Lecture, ``notebook-`` -> Notebook, ``sample-`` ->
    Samples (+ FORCED_SAMPLES names); archived repos, repos carrying the
    ``status-legacy`` topic and non-matching names are excluded entirely,
  * card content (v6.2 sober mobile-first layout): the card surface is
    tinted by category (pastel Tailwind literals baked from the
    ``CATEGORY_TINT`` dict), and the meta row carries the category label
    CHIP (``CATEGORY_CHIP`` literals) + status chip (colour-coded, dark
    text on light tint). The human description is the primary line
    (2-line clamp) and the repo name a small monospace secondary line
    linking GitHub (that name link is the single "source" affordance —
    the v6 Source footer slot was removed in v6.2). Topic pastilles are
    the repo's REAL GitHub topics minus the system ones
    (``SYSTEM_TOPIC_PREFIXES`` area-/status- and ``SYSTEM_TOPICS``
    fmt-ipynb/review/github-actions/self-hosted — labelling noise, not
    teaching subjects), sorted alphabetically and rendered as filter
    TOGGLE buttons. The uniform action row ALWAYS shows the 3 slots
    ``Stable · Latest · Branches`` in that order: a missing target
    renders as a disabled placeholder (unfocusable
    ``<span class="lnk lnk-disabled" aria-disabled="true">`` with an
    explanatory ``title``, same box size as the enabled slots), never a
    hidden slot. Stable (solid accent) is enabled only when the repo has
    a ``v*`` tag; Branches is the native <details> disclosure (enabled
    only with >=1 non-default branch). Enabled slots keep the >=44px tap
    targets; the Branches caret sits in a fixed reserved track so the
    summary width never changes with the open/close rotation (no row
    jitter). The date line is an ABSOLUTE French date WITH time
    (``17 oct. 2026 14:32``, pushed_at converted UTC -> Europe/Paris via
    stdlib ``zoneinfo``; fixed UTC+1 + "heure locale" label when tzdata
    is unavailable) inside ``<time datetime>`` (full UTC ISO in the
    datetime attribute and the title) — no clock-dependent JS text
    anywhere (v6 removed the "il y a X" rewrite: it drifted between
    builds, user complaint).
  * filters: sticky slim header with live search; filter panel collapsed
    behind a "Filtres" toggle below lg, inline from lg up; category
    segmented control (aria-pressed), a "Thèmes…" select (options sorted
    into Fréquents/Tous les thèmes <optgroup>s, >=5 cards = frequent) as
    a keyboard-accessible way to reach any topic without hunting cards,
    sort select (JS reorders the <article> nodes) and result count.
    The default order (build order and the "Défaut" sort option) is
    deterministic: category rank (``CATEGORY_RANK``, lecture -> … ->
    student) then repo full name A->Z. Pills and the select toggle into
    the SAME active-topic Set (AND intersection semantics, one source of
    truth); an "Filtres actifs" chip row (a removable × chip per active
    topic/category/search + "Tout effacer") appears when anything is
    active. Sort, pill-toggling and the Thèmes select are progressive
    enhancement: with JS blocked the cards, links, uniform action row
    and Branches disclosures remain fully usable, the pastilles are
    INERT BUTTONS (visible topic metadata, no filtering) and the select
    is an inert control. Visibility via the Tailwind ``hidden`` utility
    (safe now that the daisyUI .card{display:flex} override is gone).

Branch/tag data is fetched per displayed repo (2 REST calls each,
ThreadPool(8)); a per-repo fetch failure degrades to a DISABLED placeholder
slot (the action row stays uniform, see _render_card). Site URLs are NOT
probed at build time; a stale link is acceptable (see the stable-pointer
note in _render_card).

Styling is a purged Tailwind v3 stylesheet built AT CI BUILD TIME from
``inputs.css`` + ``tailwind.config.js`` (content = this file) into
``public/app.css`` and linked locally — no CDN <script>/<link> in the
page, so it renders correctly with third-party/CDN requests blocked and
without FOUC. Fonts are the system stack, no images or icons. Running
``python catalog.py`` locally regenerates index.html; the CSS step is
``npx tailwindcss@3 -c tailwind.config.js -i inputs.css -o public/app.css --minify``.

Pure Python 3 stdlib. Auth: reads ``GITHUB_TOKEN`` (required) from the
environment. The ``cleanup`` subcommand (Cloudflare Pages preview pruning,
kept for ``.github/workflows/cleanup.yml``) additionally reads
``CF_API_TOKEN`` and is unaffected by the catalog rendering path above.
"""

import argparse
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

# Card timestamps are shown in Europe/Paris (spec v6.2 item 1). stdlib
# zoneinfo (Py3.9+, CI runs 3.12) resolves it from the system tzdata or the
# ``tzdata`` PyPI package; on a bare image without either, ZoneInfo raises
# ZoneInfoNotFoundError at import-eval time -> _PARIS_TZ stays None and
# _abs_datetime_fr falls back to a FIXED UTC+1 offset honestly labelled
# "heure locale" (approximate across DST, better than a silent wrong zone).
try:
    from zoneinfo import ZoneInfo
    _PARIS_TZ = ZoneInfo("Europe/Paris")
except Exception:  # noqa: BLE001 - ImportError / ZoneInfoNotFoundError / OSError
    _PARIS_TZ = None

# --- configuration --------------------------------------------------------

ORG = "ebpro"
API = "https://api.github.com"
OUT_DIR = "public"
USER_AGENT = "ebpro-teaching-catalog"

# Base under which the rendered course sites are published (deploy-static job
# of the shared Quarto render pipeline). No build-time probing (see docstring).
SITE_BASE = "https://bruno.univ-tln.fr/teaching-material"

# --- teaching surface (name-prefix classification, user spec v3) -----------

# Display surface: THREE categories, assigned by repo NAME PREFIX (v3:
# independent of topic labelling, so topic alignment work elsewhere cannot
# shift the surface):
#   lecture  : name starts with "lecture-"
#   notebook : name starts with "notebook-"
#   sample   : name starts with "sample-" + FORCED_SAMPLES
# Excluded: archived repos, anything carrying the ``status-legacy`` topic,
# and every name matching none of the prefixes above.
CATEGORIES = ("lecture", "notebook", "sample")

# Repos taught as samples regardless of their name prefix (user decision).
FORCED_SAMPLES = ("hellogit", "conflictbot")

CATEGORY_LABELS = {
    "lecture": "Lecture",
    "notebook": "Notebook",
    "sample": "Samples",
}

# Status chips (v5): sober colour-coded pastilles, dark text on a light
# tint (AA). Unknown/other statuses (frozen, duplicate, legacy…) fall back
# to chip-neutral; the card itself is a neutral white surface — the
# category is labelled in the meta row, not by card colour (single-accent
# brief: colour is reserved for actions/status).
STATUS_CHIP = {
    "active": "chip-active",
    "partial": "chip-partial",
    "stub": "chip-stub",
}

# Statuses rendered with reduced emphasis.
STATUS_MUTED = ("frozen", "duplicate")

# Topic hygiene (spec v6.2 item 5): org-labelling machinery is NOT a
# teaching subject — these topics render on neither the card pills nor the
# Thèmes select. area-* is redundant with the category label, status-*
# with the status chip; the set members are labelling/CI noise
# (fmt-ipynb is classification-only, review/github-actions/self-hosted
# are workflow markers). Genuine compound topics (ci-cd, java-25, …)
# PASS — they are real user-authored subjects.
SYSTEM_TOPIC_PREFIXES = ("area-", "status-")
SYSTEM_TOPICS = {"fmt-ipynb", "review", "github-actions", "self-hosted"}

# A topic carried by at least this many cards lands in the "Fréquents"
# optgroup of the Thèmes select (Tous les thèmes holds the remainder).
TOPIC_FREQUENT_MIN = 5

# Deterministic default order (spec v6.2 item 6): category rank first,
# then repo full name A->Z. Ranks cover the whole org taxonomy, not only
# the three rendered categories, so the baked data-catrank stays meaningful
# if the surface widens. Unknown categories rank last (11).
CATEGORY_RANK = {
    "lecture": 0,
    "notebook": 1,
    "sample": 2,
    "standard": 3,
    "reference": 4,
    "image": 5,
    "cicd": 6,
    "infra": 7,
    "meta": 8,
    "personal": 9,
    "student": 10,
    "none": 11,
}

# Pastel card tints per category (spec v6.2 item 7). PURGE-SAFE BY
# CONSTRUCTION: the values are complete literal Tailwind class strings in
# this file (the Tailwind content source), never composed from the hue —
# the CLI emits exactly these into app.css.
CATEGORY_TINT = {
    "lecture": "bg-sky-50 border-sky-200",
    "notebook": "bg-emerald-50 border-emerald-200",
    "sample": "bg-amber-50 border-amber-200",
    "standard": "bg-slate-100 border-slate-200",
    "reference": "bg-violet-50 border-violet-200",
    "image": "bg-lime-50 border-lime-200",
    "cicd": "bg-teal-50 border-teal-200",
    "infra": "bg-indigo-50 border-indigo-200",
    "meta": "bg-cyan-50 border-cyan-200",
    "personal": "bg-rose-50 border-rose-200",
    "student": "bg-fuchsia-50 border-fuchsia-200",
    "none": "bg-white border-slate-200",
}

# Category label chip colours (meta row), same literal-class rule:
# text-<hue>-800 on bg-<hue>-100 (>=7:1 on white-class tints, AA+) with a
# matching border literal for definition against the identically-hued card
# tint. slate hues get a step darker border so the chip separates from the
# standard card ground (bg-slate-100).
CATEGORY_CHIP = {
    "lecture": "text-sky-800 bg-sky-100 border-sky-200",
    "notebook": "text-emerald-800 bg-emerald-100 border-emerald-200",
    "sample": "text-amber-800 bg-amber-100 border-amber-200",
    "standard": "text-slate-800 bg-slate-100 border-slate-300",
    "reference": "text-violet-800 bg-violet-100 border-violet-200",
    "image": "text-lime-800 bg-lime-100 border-lime-200",
    "cicd": "text-teal-800 bg-teal-100 border-teal-200",
    "infra": "text-indigo-800 bg-indigo-100 border-indigo-200",
    "meta": "text-cyan-800 bg-cyan-100 border-cyan-200",
    "personal": "text-rose-800 bg-rose-100 border-rose-200",
    "student": "text-fuchsia-800 bg-fuchsia-100 border-fuchsia-200",
    "none": "text-slate-800 bg-slate-100 border-slate-200",
}


def _display_topics(topics):
    """Alphabetically sorted non-system topics of a repo (spec v6.2 item 5):
    single source of truth for card pills, the card ``data-topics`` filter
    haystack and the Thèmes select options — so what you see is what
    filters."""
    return sorted(
        t for t in (topics or [])
        if not t.startswith(SYSTEM_TOPIC_PREFIXES) and t not in SYSTEM_TOPICS)


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
_pace_lock = threading.Lock()  # pacing is global even under ThreadPool(8)


def _pace():
    """Ensure minimum interval between API calls (thread-safe)."""
    global _last_api_call
    with _pace_lock:
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


# --- per-repo branches + tags (for the card footer buttons) -----------------


def _vtag_key(tag):
    """Sort key approximating ``sort -V`` for a ``v*`` tag name."""
    parts = re.split(r"[.\-+]", tag[1:])  # drop the leading "v"
    return [(0, int(p)) if p.isdigit() else (1, p) for p in parts]


def _latest_vtag(tags):
    """Latest ``^v`` tag name among GitHub tag objects, or None."""
    names = [t.get("name") for t in tags
             if t.get("name") and re.match(r"^v", t["name"])]
    if not names:
        return None
    return max(names, key=_vtag_key)


def _fetch_repo_extras(name, default_branch):
    """Return ``(non-default branches, latest v* tag)`` for one repo.

    2 REST calls (branches + tags, per_page=100, single page). Any failure
    degrades to an empty result so the corresponding button is hidden rather
    than breaking the build.
    """
    try:
        br = _request("%s/repos/%s/%s/branches?per_page=100" % (API, ORG, name))
    except Exception as e:  # noqa: BLE001 - degrade, never break the build
        sys.stderr.write("WARN: branches fetch failed for %s (%s)\n" % (name, e))
        br = None
    try:
        tg = _request("%s/repos/%s/%s/tags?per_page=100" % (API, ORG, name))
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("WARN: tags fetch failed for %s (%s)\n" % (name, e))
        tg = None
    # Branches dropdown exclusions: the repo default branch, ``gh-pages``
    # (a render artefact, not a content branch) and the literal ``develop``
    # (safety: already the canonical Latest target, never a preview row).
    excluded = {default_branch, "gh-pages", "develop"}
    branches = sorted(b["name"] for b in (br or [])
                      if b.get("name") and b["name"] not in excluded)
    return branches, _latest_vtag(tg or [])


def fetch_extras(entries, workers=8):
    """Parallel per-repo branches+tags fetch.

    ``entries``: list of ``(repo name, default branch)``. Returns a dict
    ``name -> (branches, latest_vtag)``. ~2 calls per repo at ~5 calls/s
    global pacing -> well under the 4-minute build budget for ~60 repos.
    """
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(
            lambda e: _fetch_repo_extras(e[0], e[1]), entries))
    return dict(zip([e[0] for e in entries], results))


# --- classification helpers --------------------------------------------------


def _classify(repo):
    """Return the display category key for ``repo``, or None to exclude it.

    Rules (user spec v3, evaluated in this order):
      archived -> excluded;
      ``status-legacy`` topic -> excluded;
      FORCED_SAMPLES name -> ``sample``;
      name prefix ``lecture-`` / ``notebook-`` / ``sample-`` -> that category;
      anything else -> excluded.
    """
    if repo.get("archived"):
        return None
    name = (repo.get("name") or "").lower()
    if "status-legacy" in (repo.get("topics") or []):
        return None
    if name in FORCED_SAMPLES:
        return "sample"
    if name.startswith("lecture-"):
        return "lecture"
    if name.startswith("notebook-"):
        return "notebook"
    if name.startswith("sample-"):
        return "sample"
    return None


def _status_of(repo):
    """First ``status-*`` topic suffix, or None."""
    for t in repo.get("topics") or []:
        if t.startswith("status-"):
            return t[len("status-"):]
    return None



MONTHS_FR = ("janv.", "févr.", "mars", "avr.", "mai", "juin",
             "juil.", "août", "sept.", "oct.", "nov.", "déc.")


def _abs_datetime_fr(iso):
    """``2026-10-17T12:32:00Z`` -> ``17 oct. 2026 14:32`` (build-time).

    Spec v6.2 item 1: absolute French date PLUS HH:MM time, the pushed_at
    UTC instant converted to Europe/Paris via stdlib zoneinfo (DST-aware).
    When tzdata is unavailable (_PARIS_TZ None, bare CI/dev image) the
    conversion falls back to a fixed UTC+1 offset and the label
    ``heure locale`` is appended, so the page never asserts a precision it
    does not have. Still fully ABSOLUTE: no clock-dependent JS text
    anywhere (the v4/v5 "il y a X" rewrite drifted between builds and was
    removed in v6). The full UTC ISO timestamp travels in the ``datetime``
    /``title`` attributes of the rendered ``<time>`` element (machine
    readable, zone-explicit).
    """
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    dt = dt.replace(tzinfo=timezone.utc)
    if _PARIS_TZ is not None:
        local = dt.astimezone(_PARIS_TZ)
        return "%d %s %d %s" % (
            local.day, MONTHS_FR[local.month - 1], local.year,
            local.strftime("%H:%M"))
    local = dt.astimezone(timezone(timedelta(hours=1)))
    return "%d %s %d %s heure locale" % (
        local.day, MONTHS_FR[local.month - 1], local.year,
        local.strftime("%H:%M"))


def _gen_date_fr(iso_utc):
    """``2026-10-14T10:00:00Z`` -> ``14/10/2026 à 12:00`` (local-free, UTC)."""
    try:
        dt = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return iso_utc
    return dt.strftime("%d/%m/%Y à %H:%M UTC")


def _status_chip(status, muted=False):
    """Sober status pastille for a card meta row ('' when no status-*).

    ``muted`` (frozen/duplicate statuses) forces the neutral ``chip-muted``
    pastille instead of the status colour mapping.
    """
    if not status:
        return ""
    cls = "chip-muted" if muted else STATUS_CHIP.get(status, "chip-neutral")
    return ('<span class="chip %s">%s</span>'
            % (cls, html.escape(status)))


# --- rendering ---------------------------------------------------------------


def _disabled_slot(label, title):
    """Disabled action-slot placeholder (spec v6, uniform card row).

    Rendered as an unfocusable ``<span>`` (NOT ``<button disabled>``:
    disabled buttons drop out of the tab order but keep hover styling
    traps; the span is inert by construction). Same box as ``.lnk``
    (min-height 44px, border, padding) so the row stays visually
    uniform; ``.lnk-disabled`` recolors to slate-500 on white (AA for
    4.5:1 body text) and cancels the ``.lnk`` hover shift. ``title``
    explains the absence (e.g. "Aucune branche").
    """
    return ('<span class="lnk lnk-disabled" aria-disabled="true" '
            'title="%s">%s</span>' % (html.escape(title, quote=True),
                                      html.escape(label)))


def _render_card(c):
    """Return the <article> for one catalog entry ``c`` (v6.2 sober layout).

    Hierarchy: the card ground is a pastel tint keyed to the category
    (``CATEGORY_TINT`` literal classes, purge-safe); a meta row carries
    the category label CHIP (``CATEGORY_CHIP`` literals) + status chip;
    then the human description as the primary line (line-clamp-2, links
    to the primary site path); the repo name as a small monospace
    secondary line linking GitHub (this name link is the source
    affordance — the v6 Source footer slot was removed in v6.2). Topics
    are the non-system GitHub topics (see _display_topics), sorted
    alphabetically, as neutral monospace filter TOGGLE buttons
    (``data-topic`` + ``aria-pressed``; the JS toggles them into the
    active topic set — progressive enhancement, INERT buttons without
    JS). The footer is a UNIFORM 3-slot action row, always in the order
    Stable · Latest · Branches (spec v6.2 item 2): an unavailable target
    (no v* tag / no non-default branch) renders a disabled placeholder
    slot instead of disappearing. Enabled slots are >=44px tap targets.
    The Branches <summary> caret lives in a fixed reserved track so the
    disclosure never changes row width (.chev::after, inputs.css). The
    date line is an absolute French date WITH HH:MM time inside ``<time
    datetime>`` (full UTC ISO in the datetime/title attributes, Europe/
    Paris display, no JS clock text). ``data-title`` (lower-cased name),
    ``data-ts`` (pushed_at ISO) and ``data-catrank`` (category rank int,
    spec v6.2 item 6) on the <article> back the JS sort control.
    """
    cat = c["category"]
    # Muted statuses (frozen/duplicate) are de-emphasised LOCALLY: a neutral
    # chip-muted status pastille. No card-level transparency (the old 60%
    # opacity washed the text below AA contrast, review P1-a). Footer links
    # keep their normal AA colours.
    is_muted = c["status"] in STATUS_MUTED
    # v6.2 AA audit (measured on the compiled palette): slate-500 lands at
    # 4.26-4.46:1 on 6 of the 12 pastel card tints -- BELOW AA. The meta
    # lines therefore use slate-600 on every card (>=6.7:1 on all tints);
    # muted vs active stays differentiated by the status pastille
    # (chip-muted vs status chip), not by washing the text. The old
    # contract idea of "darkening the tint" is directionally wrong here:
    # a darker ground lowers contrast further for dark text.
    meta_text = "text-slate-600"
    esc_name = html.escape(c["name"])
    esc_desc = html.escape(c["description"] or "")
    card_topics = _display_topics(c["topics"])
    esc_topics = html.escape(" ".join(card_topics), quote=True)
    # Search haystack (name + description, lowercased): the JS filters on
    # this plain attribute -> no JSON-in-attribute quoting bug class.
    esc_search = html.escape(
        (c["name"] + " " + (c["description"] or "")).lower(), quote=True)
    # Sort keys baked as data attributes (spec v6 item 3, rank added in
    # v6.2 item 6): data-title = lowercased repo name (stable
    # lexicographic key), data-ts = pushed_at ISO (chronological key),
    # data-catrank = category rank int (default-order key). All
    # quote-escaped; missing values -> "".
    esc_ts = html.escape(c["pushed_at"] or "", quote=True)
    parts = [
        '<article data-area="%s" data-topics="%s" data-search="%s" '
        'data-title="%s" data-ts="%s" data-catrank="%d" '
        'class="card rounded-lg border p-4 shadow-sm %s">'
        % (cat, esc_topics, esc_search,
           html.escape(c["name"].lower(), quote=True), esc_ts,
           CATEGORY_RANK.get(cat, CATEGORY_RANK["none"]),
           CATEGORY_TINT.get(cat, CATEGORY_TINT["none"])),
        '<div class="flex flex-wrap items-center gap-2">'
        '<span class="chip uppercase tracking-wide %s">%s</span>%s</div>'
        % (CATEGORY_CHIP.get(cat, CATEGORY_CHIP["none"]),
           CATEGORY_LABELS[cat].upper(), _status_chip(c["status"], is_muted)),
    ]
    # Primary line links to the site: ``stable/`` when the repo has a v* tag
    # (same condition as the Stable button), ``develop/`` otherwise.
    primary_url = "%s/%s/%s/" % (
        SITE_BASE, c["name"], "stable" if c["latest_vtag"] else "develop")
    if esc_desc:
        parts.append(
            '<a href="%s" target="_blank" rel="noopener" '
            'class="mt-1 block text-base font-medium leading-snug '
            'text-slate-900 line-clamp-2 hover:text-indigo-700 '
            'hover:underline">%s</a>' % (primary_url, esc_desc))
        parts.append(
            '<a href="%s" target="_blank" rel="noopener" '
            'class="mt-1 block break-all font-mono text-[13px] %s '
            'hover:text-indigo-700">%s</a>'
            % (c["github_url"], meta_text, esc_name))
    else:
        parts.append(
            '<a href="%s" target="_blank" rel="noopener" '
            'class="mt-1 block break-all text-base font-semibold '
            'text-slate-900 hover:text-indigo-700 hover:underline">'
            '%s</a>' % (primary_url, esc_name))
    # Topic pastilles = filter TOGGLES (spec v6 item 4, hygiene v6.2 item
    # 5): the repo's non-system topics (see _display_topics — area-*,
    # status-* and the SYSTEM_TOPICS markers are labelling machinery, not
    # subjects, and render nowhere), sorted alphabetically, as neutral
    # monospace BUTTONS carrying data-topic + aria-pressed="false". The
    # JS toggles the topic into the single active-topic Set (shared with
    # the Thèmes select and the × chips) and syncs every same-topic pill
    # on every card (pressed style: chip-pill-active + aria-pressed).
    # Without JS they are INERT BUTTONS (progressive enhancement) — the
    # card content and links stay fully usable; they remain the no-JS
    # visible topic metadata.
    pills = card_topics
    if pills:
        parts.append(
            '<div class="mt-3 flex flex-wrap gap-1">%s</div>'
            % "".join(
                '<button type="button" class="chip chip-topic" '
                'data-topic="%s" aria-pressed="false" '
                'title="Filtrer sur le thème : %s">%s</button>'
                % (html.escape(t, quote=True), html.escape(t, quote=True),
                   html.escape(t))
                for t in pills))

    # Uniform action row (spec v6 item 1, Source slot removed in v6.2
    # item 2): ALWAYS the 3 slots in the order Stable · Latest · Branches.
    # A missing target is a disabled placeholder (unfocusable span,
    # aria-disabled, explanatory title), never a hidden slot, so every
    # card row has the same shape. The GitHub source stays one click away
    # via the repo-name subtitle link. The row is flex-nowrap (measured:
    # the three slots fit one line inside a 360px card; gap tightens below
    # sm as margin) and the Branches caret occupies a fixed track (v6.2
    # item 4) so opening the disclosure never re-wraps the row.
    links = []
    # Stable: solid accent, only enabled when the repo has a v* tag; points
    # at the canonical ``stable/`` pointer path written by the deploy-static
    # job (v1.3.23+) on v* tag deploys.
    if c["latest_vtag"]:
        links.append(
            '<a class="lnk lnk-primary" title="Dernière version : %s" '
            'href="%s/%s/stable/" target="_blank" rel="noopener">'
            'Stable</a>'
            % (html.escape(c["latest_vtag"]), SITE_BASE, c["name"]))
    else:
        links.append(_disabled_slot("Stable", "Aucune version publiée"))
    # Latest = the develop render (always enabled: all three categories are
    # Quarto render-callers).
    links.append(
        '<a class="lnk" href="%s/%s/develop/" target="_blank" '
        'rel="noopener">Latest</a>' % (SITE_BASE, c["name"]))
    # Branches: native <details> disclosure of the non-default branches,
    # each linking the previews/<sanitized>/ path (branch "/" -> "-").
    # Disabled placeholder when the fetch failed or the repo has no
    # non-default branch (works without JS either way).
    if c["branches"]:
        items = "".join(
            '<li><a class="block min-h-11 whitespace-nowrap px-3 py-3 font-mono '
            'text-[13px] text-slate-700 hover:bg-slate-100 '
            'hover:text-indigo-700" '
            'href="%s/%s/previews/%s/" target="_blank" rel="noopener">'
            '%s</a></li>'
            % (SITE_BASE, c["name"], html.escape(b.replace("/", "-")),
               html.escape(b))
            for b in c["branches"])
        links.append(
            '<details class="dd"><summary class="lnk chev" '
            'aria-expanded="false">Branches</summary>'
            '<ul class="dd-list max-h-64 overflow-y-auto py-1">%s</ul>'
            '</details>' % items)
    else:
        links.append(_disabled_slot("Branches", "Aucune branche"))
    parts.append(
        '<footer class="mt-4 flex flex-nowrap items-center '
        'gap-1.5 sm:gap-2">%s</footer>' % "".join(links))
    # Date: ABSOLUTE French date + HH:MM (Europe/Paris at build time,
    # spec v6.2 item 1). The <time> element carries the full UTC ISO
    # timestamp in datetime (and title as a plain-text fallback); no JS
    # rewrites clock-dependent text anywhere on the card.
    abs_date = _abs_datetime_fr(c["pushed_at"])
    if abs_date:
        parts.append(
            '<div class="mt-3 text-[13px] %s">Mis à jour '
            '<time datetime="%s" title="%s">%s</time></div>'
            % (meta_text, esc_ts, esc_ts, html.escape(abs_date)))
    parts.append("</article>")
    return "\n      ".join(parts)


def _render_index(cards, generated_at):
    """Return the full index.html source (v6: purged local app.css, no CDN).

    The stylesheet is generated at CI build time from inputs.css +
    tailwind.config.js into public/app.css and linked relatively (the page
    is deployed flat under catalog/, so "app.css" resolves next to
    index.html). Layout is mobile-first: sticky 56px header carrying the
    live search, filter panel collapsed behind a "Filtres" toggle below lg
    and inline from lg up, 1/2/3-column card grid. The panel holds the
    category segmented control, the "Thèmes…" topic select (spec v6.2
    item 5: non-system topics only, two alphabetical <optgroup>s —
    "Fréquents" for topics present in >= TOPIC_FREQUENT_MIN cards, "Tous
    les thèmes" for the rest; a JS-only enhancement giving keyboard users
    a single place to reach any topic), the sort select and the result
    count; an "Filtres actifs" chip row sits between the panel and the
    grid, shown by JS when any topic/category/search filter is active.
    """
    count = len(cards)
    # Category segmented control: real buttons, the selected one marked
    # aria-pressed (solid accent); "Tous" active by default. State lives in
    # the attribute, never in a style class (no class-sniffing fragility).
    tally = {a: sum(1 for c in cards if c["category"] == a)
             for a in CATEGORIES}
    chip_btns = [
        '<button type="button" data-cat="all" aria-pressed="true" '
        'class="f-btn">Tous (%d)</button>' % count]
    for a in CATEGORIES:
        chip_btns.append(
            '<button type="button" data-cat="%s" aria-pressed="false" '
            'class="f-btn">%s (%d)</button>'
            % (a, CATEGORY_LABELS[a], tally[a]))
    # Thèmes select options (spec v6.2 item 5): card-frequency tally over
    # the non-system topics, split into two alphabetically sorted groups.
    # Option texts/values are html-escaped (topics are user-authored).
    topic_counts = {}
    for c in cards:
        for t in _display_topics(c["topics"]):
            topic_counts[t] = topic_counts.get(t, 0) + 1
    frequent = sorted(t for t, n in topic_counts.items()
                      if n >= TOPIC_FREQUENT_MIN)
    others = sorted(t for t, n in topic_counts.items()
                    if n < TOPIC_FREQUENT_MIN)

    def _optgroup(label, ts):
        if not ts:
            return ""
        return ('<optgroup label="%s">%s</optgroup>'
                % (html.escape(label, quote=True),
                   "".join('<option value="%s">%s</option>'
                           % (html.escape(t, quote=True), html.escape(t))
                           for t in ts)))

    topic_opts = ('<option value="">Thèmes…</option>'
                  + _optgroup("Fréquents", frequent)
                  + _optgroup("Tous les thèmes", others))
    grid = "\n      ".join(_render_card(c) for c in cards)
    return """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>Supports pédagogiques · Emmanuel BRUNO</title>
  <!-- Tailwind v3 purgé, généré au build CI (inputs.css -> public/app.css).
       Aucun CDN : la page est mise en page intégralement même si les
       requêtes tierces sont bloquées et sans FOUC. -->
  <link rel="stylesheet" href="app.css">
</head>
<body class="min-h-screen bg-slate-50 text-slate-900 antialiased">
  <a class="skip-link" href="#grid">Aller au catalogue</a>
  <header class="sticky top-0 z-40 h-14 border-b border-slate-200 bg-white">
    <div class="mx-auto flex h-full max-w-6xl items-center gap-3 px-4">
      <h1 class="min-w-0 truncate text-lg font-semibold tracking-tight">Supports pédagogiques <span class="hidden sm:inline">d’Emmanuel BRUNO</span></h1>
      <input id="f-search" type="search" placeholder="Rechercher un support…"
             class="ml-auto h-11 w-full max-w-[10rem] rounded-md border border-slate-300 bg-white px-3 text-sm sm:max-w-xs"
             aria-label="Rechercher un support">
    </div>
  </header>
  <main class="mx-auto max-w-6xl px-4">
    <section class="py-6">
      <p class="max-w-3xl text-base text-slate-600">Des cours complets (lecture), des modules indépendants (notebook) et des exemples (samples).</p>
      <a class="lnk mt-3" href="mailto:emmanuel.bruno@univ-tln.fr">emmanuel.bruno@univ-tln.fr</a>
    </section>
    <nav aria-label="Filtres" id="filters" class="pb-2">
      <button id="f-toggle" type="button" class="f-btn lg:hidden"
              aria-expanded="false" aria-controls="filters-body">Filtres</button>
      <div id="filters-body" class="hidden mt-3 flex flex-col gap-3 rounded-lg border border-slate-200 bg-white p-3 lg:flex lg:flex-row lg:flex-wrap lg:items-center">
        <div role="group" aria-label="Catégorie" class="flex flex-wrap gap-2">
          %s
        </div>
        <div class="flex flex-wrap items-center gap-3 lg:ml-auto">
          <!-- Thèmes (spec v6.2 item 5): JS-only enhancement — it toggles
               into the same active-topic Set as the card pills; without
               JS it is inert (the pills remain the visible metadata). -->
          <select id="f-topic" aria-label="Filtrer par thème"
                  class="h-11 w-full max-w-[12rem] rounded-md border border-slate-300 bg-white px-3 text-sm font-medium text-slate-700">
            %s
          </select>
          <label class="flex items-center gap-2 text-sm font-medium text-slate-600">Trier
            <select id="f-sort"
                    class="h-11 rounded-md border border-slate-300 bg-white px-3 text-sm font-medium text-slate-700">
              <option value="">Défaut (catégorie → nom)</option>
              <option value="title-asc">Titre A→Z</option>
              <option value="title-desc">Titre Z→A</option>
              <option value="date-desc">Date récente</option>
              <option value="date-asc">Date ancienne</option>
            </select>
          </label>
          <button id="f-reset" type="button" class="f-btn">Réinitialiser</button>
          <span id="result-count" class="chip chip-neutral font-mono">%d/%d</span>
        </div>
      </div>
    </nav>
    <!-- Filtres actifs (spec v6 item 4): shown by JS when a topic, the
         category or the search is active; each active filter is a
         removable × chip built by JS from DOM nodes (no innerHTML, so no
         injection surface). Hidden state = the "hidden" utility. -->
    <div id="active-filters" class="hidden flex flex-wrap items-center gap-2 py-2">
      <span class="text-[13px] font-medium uppercase tracking-wide text-slate-600">Filtres actifs :</span>
      <span id="active-filters-chips" class="flex flex-wrap items-center gap-2"></span>
      <button id="af-clear" type="button" class="chip chip-filter-x">Tout effacer</button>
    </div>
    <div id="grid"
         class="grid grid-cols-1 gap-4 py-6 sm:grid-cols-2 lg:grid-cols-3">
      %s
    </div>
    <p id="empty" class="hidden py-10 text-center text-slate-500">Aucun support ne correspond à ces filtres.</p>
  </main>
  <footer class="mx-auto flex max-w-6xl flex-wrap items-center gap-x-4 gap-y-1 px-4 pb-10 text-[13px] text-slate-500">
    <span>Généré le %s</span>
    <span>%d dépôts</span>
    <span>source : <a class="text-indigo-700 hover:underline" href="https://github.com/ebpro/teaching-catalog">teaching-catalog</a></span>
    <span>Contact : <a class="text-indigo-700 hover:underline" href="mailto:emmanuel.bruno@univ-tln.fr">emmanuel.bruno@univ-tln.fr</a></span>
  </footer>
  <script>
    // Filtres v6.2 : recherche + catégorie (groupe exclusif, aria-pressed)
    // + thèmes activés par les PASTILLES des cartes ET par la sélection
    // "Thèmes" du panneau — les deux basculent dans le MÊME Set
    // "activeTopics" (intersection AND, source de vérité unique) + tri JS
    // (replace les <article> dans le DOM). La visibilité des cartes est
    // pilotée par la classe utilitaire "hidden" (display:none). Enrichis-
    // sement progressif : sans JS, cartes/liens/dépliants Branches restent
    // pleinement utilisables ; les pastilles sont des BOUTONS INERTES
    // (métadonnée de thèmes visible, sans filtrage) et la sélection
    // "Thèmes" est un contrôle inerte ; l'ordre de construction (rang de
    // catégorie puis nom complet A→Z, spec v6.2 item 6) s'applique.
    // Les dates sont absolues au build (date FR + heure Europe/Paris) :
    // AUCUN texte dépendant de l'horloge n'est généré côté client (la
    // réécriture relative v4/v5 dérivait entre deux déploiements —
    // retirée en v6).
    const grid = document.getElementById('grid');
    const cards = [...grid.querySelectorAll('article')];
    const originalOrder = [...cards];      // ordre de construction (reset du tri)
    const catBtns = [...document.querySelectorAll('#filters [data-cat]')];
    const search = document.getElementById('f-search');
    const countEl = document.getElementById('result-count');
    const emptyEl = document.getElementById('empty');
    const sortSel = document.getElementById('f-sort');
    const topicSel = document.getElementById('f-topic');
    const afWrap = document.getElementById('active-filters');
    const afChips = document.getElementById('active-filters-chips');
    const total = cards.length;
    let cat = "all";
    const activeTopics = new Set();

    // <details>/<summary> : aria-expanded synchronisé sur l'état natif.
    document.querySelectorAll('details').forEach(d => {
      const s = d.querySelector('summary[aria-expanded]');
      if (s) d.addEventListener("toggle", () =>
        s.setAttribute("aria-expanded", String(d.open)));
    });

    // Filtre AND : catégorie x recherche x intersection des thèmes.
    function applyFilters() {
      const q = (search.value || "").trim().toLowerCase();
      const sel = [...activeTopics];
      let visible = 0;
      cards.forEach(c => {
        const topics = (c.dataset.topics || "").split(" ");
        const ok = (cat === "all" || c.dataset.area === cat)
          && (!q || (c.dataset.search || "").includes(q))
          && sel.every(t => topics.includes(t));
        c.classList.toggle("hidden", !ok);
        if (ok) visible++;
      });
      countEl.textContent = visible + "/" + total;
      emptyEl.classList.toggle("hidden", visible !== 0);
    }

    // Tri (spec v6 item 3) : réordonne TOUTES les cartes (visibles et
    // masquées) en déplaçant les <article> existants, puis rejoue les
    // filtres. Clés = data-title (nom minuscule) / data-ts (ISO pushed_at,
    // tri lexicographique = tri chronologique). Aucun : "value" vide =
    // ordre de construction. Pas d'animation (préférence sobre + pas de
    // conflit prefers-reduced-motion).
    function applySort() {
      const mode = sortSel.value;
      const list = [...cards];
      if (mode) {
        const key = mode.startsWith("title")
          ? (c => c.dataset.title || "")
          : (c => c.dataset.ts || "");
        list.sort((a, b) => {
          const ka = key(a), kb = key(b);
          const r = ka < kb ? -1 : (ka > kb ? 1 : 0);
          return mode.endsWith("-desc") ? -r : r;
        });
      } else {
        list.sort((a, b) =>
          originalOrder.indexOf(a) - originalOrder.indexOf(b));
      }
      list.forEach(c => grid.appendChild(c));
      applyFilters();
    }

    // Reflète l'état du Set sur TOUTES les pastilles du même thème
    // (aria-pressed = état, chip-pill-active = style pressé).
    function syncPills() {
      grid.querySelectorAll("button[data-topic]").forEach(p => {
        const on = activeTopics.has(p.dataset.topic);
        p.setAttribute("aria-pressed", String(on));
        p.classList.toggle("chip-pill-active", on);
      });
    }

    // Puce "Filtres actifs" retirables : noeuds DOM construits à la main
    // (createElement/textContent, jamais innerHTML) -> pas de surface
    // d'injection même pour les valeurs saisies dans la recherche.
    function xChip(labelText, ariaPrefix, onRemove) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip chip-filter-x";
      b.setAttribute("aria-label", ariaPrefix + " " + labelText);
      b.textContent = labelText;
      const x = document.createElement("span");
      x.className = "ml-2";
      x.setAttribute("aria-hidden", "true");
      x.textContent = "\u00d7";
      b.appendChild(x);
      b.addEventListener("click", onRemove);
      return b;
    }

    function renderActiveFilters() {
      afChips.textContent = "";
      const q = (search.value || "").trim();
      if (cat !== "all") {
        const btn = catBtns.find(b => b.dataset.cat === cat);
        const label = btn ? btn.textContent.split(" (")[0] : cat;
        afChips.appendChild(xChip(label, "Retirer la catégorie",
          () => setCat("all")));
      }
      if (q) {
        afChips.appendChild(xChip(q, "Retirer la recherche", () => {
          search.value = "";
          update();
        }));
      }
      [...activeTopics].sort().forEach(t => {
        afChips.appendChild(xChip(t, "Retirer le thème", () => {
          activeTopics.delete(t);
          update();
        }));
      });
      afWrap.classList.toggle("hidden",
        cat === "all" && !q && activeTopics.size === 0);
    }

    // Le select "Thèmes" est une UI mono-sélection sur le Set multi
    // activeTopics : il reflète sa PROPRE dernière sélection (l'option
    // du thème actif reste affichée comme sélectionnée) et se
    // réinitialise dès que ce thème quitte le Set — puce × retirée,
    // pastille décochée ou réinitialisation (spec v6.2 item 5 : pills +
    // select + chips = un seul état).
    function syncTopicSelect() {
      if (topicSel.value && !activeTopics.has(topicSel.value)) {
        topicSel.value = "";
      }
    }

    function update() {
      syncPills();
      syncTopicSelect();
      renderActiveFilters();
      applyFilters();
    }

    function setCat(v) {
      cat = v;
      catBtns.forEach(x =>
        x.setAttribute("aria-pressed", String(x.dataset.cat === v)));
      update();
    }

    catBtns.forEach(b => b.addEventListener("click", () => setCat(b.dataset.cat)));
    search.addEventListener("input", () => {
      renderActiveFilters();
      applyFilters();
    });
    sortSel.addEventListener("change", applySort);
    // Sélection "Thèmes" : bascule le thème dans le MÊME Set que les
    // pastilles (sémantique AND inchangée, spec v6.2 item 5). Choisir un
    // thème inactif l'ajoute (son option reste sélectionnée) ; le
    // re-sélectionner depuis la liste le retire (syncTopicSelect remet
    // alors le placeholder). Les thèmes activés via les pastilles
    // s'ajoutent au Set sans passer par le select, qui ne peut en
    // afficher qu'un.
    topicSel.addEventListener("change", () => {
      const t = topicSel.value;
      if (!t) return;
      if (activeTopics.has(t)) activeTopics.delete(t);
      else activeTopics.add(t);
      update();
    });
    // Pastille de carte = bascule du thème dans le filtre actif
    // (mêmes sémantiques d'intersection que les puces × qui retirent
    // du même Set — source de vérité unique).
    grid.addEventListener("click", e => {
      const p = e.target.closest("button[data-topic]");
      if (!p) return;
      const t = p.dataset.topic;
      if (activeTopics.has(t)) activeTopics.delete(t);
      else activeTopics.add(t);
      update();
    });
    function clearFilters() {
      search.value = "";
      activeTopics.clear();
      setCat("all");   // -> update() complet (puces + pastilles + filtres)
    }
    document.getElementById("f-reset").addEventListener("click", clearFilters);
    document.getElementById("af-clear").addEventListener("click", clearFilters);
    // Panneau de filtres : masqué sous lg derrière le bouton "Filtres"
    // (aria-expanded), inline dès lg (classes lg:* — visible même sans JS).
    const fToggle = document.getElementById("f-toggle");
    const fBody = document.getElementById("filters-body");
    fToggle.addEventListener("click", () => {
      const open = fBody.classList.toggle("hidden") === false;
      fToggle.setAttribute("aria-expanded", String(open));
    });
    update();   // puces × synchronisées sur l'état initial (rangée masquée)
  </script>
</body>
</html>
 """ % ("\n          ".join(chip_btns), topic_opts, count, count, grid,
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
        cat = _classify(r)
        if cat is None:
            continue
        cards.append({
            "name": r["name"],
            "description": r.get("description"),
            "topics": r.get("topics") or [],
            "category": cat,
            "default_branch": r.get("default_branch"),
            "private": bool(r.get("private")),
            "status": _status_of(r),
            "ipynb": "fmt-ipynb" in (r.get("topics") or []),
            "pushed_at": r.get("pushed_at"),
            "updated": _abs_datetime_fr(r.get("pushed_at")),
            "github_url": r.get("html_url")
                          or "https://github.com/%s/%s" % (ORG, r["name"]),
        })

    # Per-repo branches + v* tags for the footer buttons (ThreadPool(8),
    # 2 REST calls per displayed repo; failures hide the buttons).
    extras_started = time.time()
    extras = fetch_extras([(c["name"], c["default_branch"]) for c in cards])
    for c in cards:
        branches, latest_vtag = extras.get(c["name"], ([], None))
        c["branches"] = branches
        c["latest_vtag"] = latest_vtag
    print("Branches/tags: %d repos in %.1fs" %
          (len(cards), time.time() - extras_started))

    # Deterministic default order (spec v6.2 item 6): full-taxonomy
    # category rank, then repo full name A->Z (lowercased key matching
    # data-title; raw name as tie-breaker). The JS "Défaut" option and the
    # originalOrder snapshot reuse this DOM order as-is.
    cards.sort(key=lambda c: (
        CATEGORY_RANK.get(c["category"], CATEGORY_RANK["none"]),
        c["name"].lower(), c["name"]))

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
    sys.stderr.write("Wrote %s/ (static card grid, %d repos; build the "
                     "purged CSS with the tailwindcss CLI step)\n"
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
