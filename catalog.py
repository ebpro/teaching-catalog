#!/usr/bin/env python3
"""teaching-catalog generator.

Scans the ``ebpro`` GitHub org (``GET /orgs/ebpro/repos``) and renders a
self-contained static card grid of the teaching surface under ``./public/``:

  * three display categories assigned by repo NAME PREFIX (spec v3):
    ``lecture-`` -> Lecture, ``notebook-`` -> Notebook, ``sample-`` ->
    Samples (+ FORCED_SAMPLES names); archived repos, repos carrying the
    ``status-legacy`` topic and non-matching names are excluded entirely,
  * card content (v8 ratified card anatomy): the card is a flex column
    whose footer is pinned to the bottom (``mt-auto``) above a hairline
    ``border-top``, so footers align across the grid. HEADER: the human
    description as the primary semibold line (2-line clamp) + the repo
    slug as a small monospace (12px) secondary line linking GitHub (the
    name link stays the single source affordance - no separate Source
    slot, v6.2). META ROW: category label CHIP (``CATEGORY_CHIP``
    literals) + status pastille (tonal variant when one exists,
    ``chip-outline`` otherwise) on the LEFT; the content date
    right-aligned in a FIXED position on the RIGHT - a
    ``<time data-commit>`` carrying the displayed branch tip ISO: the
    visible text is the absolute French date+time built by
    ``_abs_datetime_fr`` (correct without JS, kept in the ``title``),
    which the load-time JS rewrites to "il y a X" recomputed per view
    (the v6 removal targeted BUILD-time-baked relative text drifting
    between cron runs - recomputation at load cannot drift); a tap on the
    non-link date slot toggles relative <-> absolute via the baked
    ``data-abs`` string (v8.2 touch fallback). BODY: topic
    pastilles - the repo's REAL non-system GitHub topics (see
    ``_display_topics``), sorted alphabetically, as filter TOGGLE
    buttons (``data-topic`` + ``aria-pressed``; unchanged from v7).
    FOOTER: the ``Branches`` <details> disclosure as a full-width
    subdued text-link + chevron (``.dd-link``, 44px), each branch row
    carrying its own per-branch content date (same data-commit
    pattern); a 1fr/1fr action grid of 44px buttons - ``Stable``
    (``.btn-primary`` solid accent, only when the repo has a ``v*`` tag
    whose ``releases/`` render is confirmed live at build time; an
    inert grey placeholder when the tag exists, the render is not live,
    and ``develop`` is - v8.2)
    BEFORE ``Latest`` (``.btn`` outline, ONLY when the repo has a
    ``develop`` branch - v8.1 ruling: Latest is strictly the develop
    tip, no default-branch fallback; the button is omitted when the
    branch is absent, and a lone remaining button spans the full width);
    content dates live ONLY in the button
    tooltips (absolute French dates baked at build time - v8.2
    ratified spec, no load-time tooltip rewrite), never inside the
    labels; then the conditional
    ``nouveau depuis la stable`` (Latest tip newer than the stable tag
    commit) / ``build en attente`` (content date postdates catalog
    generation) chips - zero noise otherwise. The pastel category tint
    (``CATEGORY_TINT``) and every v7 sort/filter data attribute
    (``data-title``/``data-ts``/``data-catrank``/``data-formation``/
    ``data-filiere``/``data-sortkey``) are unchanged.
  * filters: sticky slim header with live search; filter panel collapsed
    behind a "Filtres" toggle below lg, inline from lg up; category
    segmented control (aria-pressed), a "Thèmes…" select (options sorted
    into Fréquents/Tous les thèmes <optgroup>s, >=5 cards = frequent) as
    a keyboard-accessible way to reach any topic without hunting cards,
    "Formation" / "Filière" selects (spec v7: options derived from the
    formation-/filiere- topics with human labels; the filière list is
    narrowed by the JS to the selected formation), a "Grouper" select
    (spec v7: JS inserts ``.group-h`` headers above the first visible
    card of each formation / formation+filière group), sort select (JS
    reorders the <article> nodes) and result count.
    The default order (build order and the "Défaut" sort option) is
    deterministic: category rank (``CATEGORY_RANK``, lecture -> … ->
    student) then, for lecture cards, formation -> filière -> name and
    for every other card the full name A->Z (baked as ``data-sortkey``).
    Pills and the Thèmes select toggle into the SAME active-topic Set
    (AND intersection semantics, one source of truth); formation and
    filière are AND-combined single-value filters (spec v7); an
    "Filtres actifs" chip row (a removable × chip per active
    topic/category/search/formation/filière + "Tout effacer") appears
    when anything is active. Sort, pill-toggling, the
    Thèmes/Formation/Filière selects, the clear-on-incompatible
    narrowing and the group headers are progressive
    enhancement: with JS blocked the cards, links, uniform action row
    and Branches disclosures remain fully usable, the pastilles are
    INERT BUTTONS (visible topic metadata, no filtering), the selects
    are inert controls and NO group headers are rendered; the static
    build order (lecture cards already ordered formation -> filière ->
    name) stands without JS. Visibility via the Tailwind ``hidden`` utility
    (safe now that the daisyUI .card{display:flex} override is gone).

Branch/tag/content-date data is fetched per displayed repo (2 base REST
calls + one commits call per dated ref: Latest ref (``develop``, only when
the repo has that branch - v8.1), stable tag, at most 12
disclosed branches; ThreadPool(8), global 5 calls/s pacing); a failed
date degrades to the repo pushed_at with a WARN (see _render_card). The
``releases/`` site family IS probed at build time (v8.2): one plain GET
per tagged repo (deduped by repo) against the publish host - the host
catch-all answers HTTP 200 for every path, so liveness keys on the body
signature (> 24 KB or a Quarto generator marker), never on the status
code; any probe failure means "not live" (see _releases_live).

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
import urllib.parse
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

# Base under which the rendered course sites are published (deploy-static
# job of the shared Quarto render pipeline). Real families (v8.2):
# ``<repo>/<default-branch>/`` (e.g. develop), ``<repo>/previews/<slug>/``
# (non-default branches, ephemeral - purged with the branch) and
# ``<repo>/releases/<tag>/`` (tags). ``stable/`` does NOT exist; the
# releases/ family is liveness-probed at build time (see _releases_live).
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
SYSTEM_TOPIC_PREFIXES = ("area-", "status-", "formation-", "filiere-")
SYSTEM_TOPICS = {"fmt-ipynb", "review", "github-actions", "self-hosted"}

# Teaching axes (spec v7): formation-/filiere- topics are system metadata
# (never pills), but they drive the Formation/Filière selects, the
# grouping headers and the lecture default order. Slugs render with these
# human labels; unknown slugs fall back to the raw slug.
FORMATION_LABELS = {"cnam": "CNAM", "utln": "UTLN"}
FILIERE_LABELS = {"mi-a2": "MI A2", "m1-info-math": "M1 Info Math",
                  "m2-info-math": "M2 Info Math", "lp-ecmn": "LP ECMN"}


def _axis_of(topics, prefix):
    """First ``prefix``-tagged topic of a repo with the prefix stripped,
    or "" when no such topic is present (spec v7 item 2)."""
    for t in topics or []:
        if t.startswith(prefix):
            return t[len(prefix):]
    return ""


def _axis_label(labels, slug):
    """Human label for an axis slug ("" stays "")."""
    return labels.get(slug, slug) if slug else ""


def _default_sortkey(c):
    """Default-order key (spec v7 item 5): lecture cards sort by
    formation -> filière -> lowercased name; every other card by name."""
    if c["category"] == "lecture":
        return "%s|%s|%s" % (c.get("formation", ""), c.get("filiere", ""),
                             c["name"].lower())
    return c["name"].lower()

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


def _request(url, attempts=_MAX_ATTEMPTS):
    """GET ``url`` and return the parsed JSON body.

    Retries with exponential backoff on transient failures (HTTP 403/429 rate
    limit, or a network-level blip such as a DNS/connection error), up to
    ``attempts`` total attempts, then raises RuntimeError (non-zero exit).
    Non-retryable HTTP errors fail immediately.
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



# --- Cloudflare Pages API layer (cleanup subcommand only; never raises) ---
#
# Reached only through ``catalog.py cleanup``
# (.github/workflows/cleanup.yml), and only when CF_API_TOKEN is set;
# the catalog rendering path never touches CF. Locally the token is
# empty and no CF calls are made. Every CF failure is swallowed so a
# Cloudflare problem can never break the (GitHub-backed) cleanup run.

CF_TOKEN = ""
CF_API = "https://api.cloudflare.com/client/v4"
_cf_call_count = 0
_cf_account_id = None
_cf_account_fetched = False
_cf_accounts_status = None
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


# Per-repo content-date budget: the disclosed branch list is fetched in
# full, but per-branch dates cost one extra commits call each (the
# branches API carries no dates), so they are fetched for at most the
# first MAX_DATED_BRANCHES disclosed branches. Any date fetch failure
# degrades to the repo pushed_at (a WARN is logged per failed ref).
MAX_DATED_BRANCHES = 12


def _commit_date(name, ref):
    """Commit date (ISO) of the tip of ``ref`` (branch or tag name).

    ``GET /repos/{org}/{repo}/commits?sha={ref}&per_page=1`` -> the
    ``commit.committer.date`` of the tip commit (the branches list
    endpoint carries no dates). None on any failure (gone ref, empty
    repo, rate-limit give-up) so callers fall back to pushed_at.
    """
    try:
        data = _request("%s/repos/%s/%s/commits?sha=%s&per_page=1" % (
            API, ORG, name, urllib.parse.quote(ref, safe="")))
    except Exception as e:  # noqa: BLE001 - degrade, never break the build
        sys.stderr.write("WARN: commit-date fetch failed for %s@%s (%s)\n"
                         % (name, ref, e))
        return None
    try:
        return data[0]["commit"]["committer"]["date"]
    except (TypeError, IndexError, KeyError):
        return None


def _fetch_repo_extras(name, default_branch):
    """Return ``(branches, latest_vtag, dates)`` for one repo.

    Base: 2 REST calls (branches + tags, per_page=100, single page), as
    before. Content dates (v8 anatomy): one extra commits call each for
    the Latest ref, the latest ``v*`` tag when present (the Stable
    content date), and at most MAX_DATED_BRANCHES of the disclosed
    branches. v8.1 user ruling: the Latest ref is STRICTLY ``develop`` -
    set only when ``develop`` is in the repo's branches list; when the
    repo has no develop branch both ``latest_ref`` and ``latest`` are
    None and NO date call is made for any fallback ref (the Latest
    affordance is then simply absent). ``dates``:
    ``{"latest_ref", "latest", "stable", "branches": {name: iso}}`` with
    None for every ref whose fetch failed (renderer falls back).
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
    raw = [b["name"] for b in (br or []) if b.get("name")]
    excluded = {default_branch, "gh-pages", "develop"}
    branches = sorted(b for b in raw if b not in excluded)
    latest_vtag = _latest_vtag(tg or [])
    # v8.1 (user ruling): Latest is ALWAYS the develop tip - never
    # another branch. No develop branch -> no Latest ref, no date call,
    # and the renderer omits the Latest button entirely.
    latest_ref = "develop" if "develop" in raw else None
    dates = {
        "latest_ref": latest_ref,
        "latest": _commit_date(name, latest_ref) if latest_ref else None,
        "stable": _commit_date(name, latest_vtag) if latest_vtag else None,
        "branches": {},
    }
    for b in branches[:MAX_DATED_BRANCHES]:
        d = _commit_date(name, b)
        if d:
            dates["branches"][b] = d
    return branches, latest_vtag, dates


def fetch_extras(entries, workers=8):
    """Parallel per-repo branches+tags+content-dates fetch.

    ``entries``: list of ``(repo name, default branch)``. Returns a dict
    ``name -> (branches, latest_vtag, dates)``. 2 base calls + up to
    2 + MAX_DATED_BRANCHES commits calls per repo, all through the
    global 5 calls/s pacing; for the ~60-repo teaching surface this
    stays far under the 5000/hour primary rate limit (6h cron).
    """
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(
            lambda e: _fetch_repo_extras(e[0], e[1]), entries))
    return dict(zip([e[0] for e in entries], results))


# --- releases/ liveness probe (v8.2) ----------------------------------------

# The publish host answers HTTP 200 with a ~23 KB fallback page for ANY
# path (DokuWiki catch-all), so a 200 is never evidence of a published
# render. Live signature: body larger than this byte floor OR carrying
# the Quarto generator marker (genuine rendered pages always do, the
# fallback page never does).
RELEASES_LIVE_MIN_BYTES = 24576
RELEASES_LIVE_MARKER = b'content="quarto-'


def _releases_live(url):
    """True when ``url`` serves a genuine rendered site (not the catch-all).

    One plain public HTTP GET (no auth, no GitHub pacing): live iff the
    body exceeds ``RELEASES_LIVE_MIN_BYTES`` or contains
    ``RELEASES_LIVE_MARKER``. ANY failure (timeout, HTTP error, DNS,
    unreadable) returns False - "not live" - and never raises, so the
    probe can only ever demote a link to the honest disabled placeholder,
    never break the build.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read(RELEASES_LIVE_MIN_BYTES + 4096)
    except Exception:  # noqa: BLE001 - probe degrades to "not live"
        return False
    if len(body) > RELEASES_LIVE_MIN_BYTES:
        return True
    return RELEASES_LIVE_MARKER in body


def probe_releases(cards, workers=8):
    """Set ``c["releases_live"]`` for every card (one probe per tagged repo).

    Deduped by repo (a card has a single candidate ``releases/<tag>/``
    URL); untagged cards get False with no call. Returns
    ``(candidates, live)`` for the build log.
    """
    candidates = {
        c["name"]: "%s/%s/releases/%s/" % (SITE_BASE, c["name"],
                                           c["latest_vtag"])
        for c in cards if c.get("latest_vtag")
    }
    live = set()
    if candidates:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for name, ok in zip(candidates, pool.map(
                    _releases_live, candidates.values())):
                if ok:
                    live.add(name)
    for c in cards:
        c["releases_live"] = c["name"] in live
    return len(candidates), len(live)


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
    does not have. v8: the load-time JS rewrites the visible text to
    "il y a X" RE-COMPUTED from the embedded ISO (``data-commit``) on
    every view - the v6 build-drift problem is structurally impossible
    here (nothing relative is ever baked). The absolute string stays in
    the ``title`` tooltip and remains the no-JS fallback. The full UTC
    ISO timestamp travels in the ``datetime``
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


def _abs_date_fr(iso):
    """``2026-10-17T12:32:00Z`` -> ``17 oct. 2026`` (build-time, date only).

    v8.2 ratified spec: the Stable/Latest button tooltips carry the
    ABSOLUTE French content DATE (no time-of-day, no load-time relative
    rewrite). Same Europe/Paris conversion and honest fixed-offset
    fallback as ``_abs_datetime_fr``.
    """
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(
        _PARIS_TZ if _PARIS_TZ is not None else timezone(timedelta(hours=1)))
    return "%d %s %d" % (local.day, MONTHS_FR[local.month - 1], local.year)


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
    cls = "chip-muted" if muted else STATUS_CHIP.get(status, "chip-outline")
    return ('<span class="chip %s">%s</span>'
            % (cls, html.escape(status)))


# --- rendering ---------------------------------------------------------------


def _date_span(iso, cls):
    """Content-date element for the v8 anatomy (meta row + branch rows).

    Same pattern everywhere: the ABSOLUTE French date+time built by
    ``_abs_datetime_fr`` is the visible text (correct with JS blocked,
    kept in the ``title`` tooltip), the raw UTC ISO travels in
    ``datetime`` AND ``data-commit`` - the load-time JS rewrites the
    text to "il y a X" RE-COMPUTED from that ISO on every view, so it
    can never drift between cron builds (ruling 1). The same absolute
    string is embedded in ``data-abs`` so a tap on the (non-link) date
    slot can toggle relative <-> absolute without any fetch (v8.2 touch
    fallback). "" when the date is unusable.
    """
    abs_dt = _abs_datetime_fr(iso)
    if not abs_dt:
        return ""
    esc_iso = html.escape(iso, quote=True)
    esc = html.escape(abs_dt)
    return ('<time class="%s" datetime="%s" data-commit="%s" data-abs="%s" '
            'title="Contenu du %s">%s</time>'
            % (cls, esc_iso, esc_iso, esc, esc, esc))


def _render_card(c, generated_at):
    """Return the <article> for one catalog entry ``c`` (v8 anatomy).

    The card is a flex column whose footer carries ``mt-auto`` above a
    hairline ``border-t`` (no fill), so footers align across the grid.
    Four zones:

      HEADER: the human description as the primary semibold line (links
        to the site: ``releases/<tag>/`` when the repo has a v* tag whose
        releases render is build-time-confirmed live, ``develop/`` when
        the repo has a develop branch, the GitHub page otherwise (v8.2
        real deploy-URL families - ``stable/`` does not exist); with no
        description the repo name becomes the primary
        line) + the repo slug as a small monospace (12px) secondary line
        linking GitHub (that name link is the single source affordance -
        no separate Source slot since v6.2).
      META ROW: category label CHIP (``CATEGORY_CHIP`` literals) + status
        pastille (tonal variant when one exists, ``chip-outline``
        otherwise) LEFT; the content date right-aligned in a FIXED
        position (``ml-auto``) on the RIGHT - ``_date_span`` over the
        displayed branch tip (``dates["latest"]``, fallback pushed_at).
      BODY: topic pastilles - the repo's non-system topics
        (``_display_topics``) as filter TOGGLE buttons (``data-topic`` +
        ``aria-pressed``; unchanged from v7, progressive enhancement -
        inert buttons without JS).
      FOOTER: the Branches <details> disclosure as a full-width subdued
        text-link + chevron (``.dd-link``, 44px), every row carrying its
        own per-branch content date; a 1fr/1fr grid (``grid-cols-2``) of
        44px action buttons (ruling 2: ``.btn`` keeps the ``min-h-11``
        tap-target size) - ``Stable`` (``.btn-primary`` solid accent,
        BEFORE ``Latest``) linking ``releases/<tag>/`` ONLY when the repo
        has a v* tag whose releases render is confirmed live at build
        time; an inert grey disabled placeholder (no href) when the tag
        exists, the render is not live, and develop is; NO Stable
        affordance when the render is not live and there is no develop
        branch. ``Latest`` rendered ONLY when the repo has a develop
        branch (v8.1 user ruling: Latest is strictly the develop tip).
        Exactly one button spans the full width (``grid-cols-1``); no
        button at all -> no action row, and with no disclosed branch
        either the whole footer is omitted (v8.2: no dangling hairline
        on footer-less cards). Content dates live ONLY in the button
        tooltips, never inside the labels, as ABSOLUTE French dates
        baked at build time (v8.2 ratified spec - the load-time
        ``data-tip-date`` rewrite is gone). Then the conditional
        chips: "nouveau depuis la stable" (Latest tip strictly newer
        than the stable tag commit, both dates fetched - never inferred)
        and "build en attente" (displayed content date postdates catalog
        generation). Zero chip noise when neither holds.

    The pastel category tint (``CATEGORY_TINT``) and every v7 sort/filter
    data attribute (``data-title``/``data-ts``/``data-catrank``/
    ``data-formation``/``data-filiere``/``data-sortkey``) are unchanged.
    """
    cat = c["category"]
    # Muted statuses (frozen/duplicate) are de-emphasised LOCALLY: a
    # chip-muted status pastille. No card-level transparency (the old 60%
    # opacity washed the text below AA contrast, review P1-a).
    is_muted = c["status"] in STATUS_MUTED
    # v6.2 AA audit: slate-600 on every card tint (>=6.7:1 on all 12).
    meta_text = "text-slate-600"
    esc_name = html.escape(c["name"])
    esc_desc = html.escape(c["description"] or "")
    card_topics = _display_topics(c["topics"])
    esc_topics = html.escape(" ".join(card_topics), quote=True)
    esc_search = html.escape(
        (c["name"] + " " + (c["description"] or "")).lower(), quote=True)
    esc_ts = html.escape(c["pushed_at"] or "", quote=True)
    esc_formation = html.escape(c.get("formation", ""), quote=True)
    esc_filiere = html.escape(c.get("filiere", ""), quote=True)
    esc_sortkey = html.escape(_default_sortkey(c), quote=True)
    # Content dates (fetched per ref in fetch_extras). A None (fetch
    # failure or no stable tag) falls back to pushed_at for DISPLAY only;
    # the raw values gate the conditional chips (no inferred "nouveau").
    dates = c.get("dates") or {}
    latest_raw = dates.get("latest")
    stable_raw = dates.get("stable")
    branch_dates = dates.get("branches") or {}
    # v8.1 (user ruling): Latest is strictly develop. dates["latest_ref"]
    # is None when the repo has no develop branch -> no Latest button is
    # rendered and no default-branch value feeds its tooltip or href.
    latest_ref = dates.get("latest_ref")
    latest_iso = latest_raw or c["pushed_at"]
    stable_iso = stable_raw or c["pushed_at"]
    parts = [
        '<article data-area="%s" data-topics="%s" data-search="%s" '
        'data-title="%s" data-ts="%s" data-catrank="%d" '
        'data-formation="%s" data-filiere="%s" data-sortkey="%s" '
        'class="card flex flex-col rounded-lg border p-4 shadow-sm %s">'
        % (cat, esc_topics, esc_search,
           html.escape(c["name"].lower(), quote=True), esc_ts,
           CATEGORY_RANK.get(cat, CATEGORY_RANK["none"]),
           esc_formation, esc_filiere, esc_sortkey,
           CATEGORY_TINT.get(cat, CATEGORY_TINT["none"])),
    ]

    # --- HEADER: description (primary) + repo slug (mono secondary) -----
    # v8.2 real deploy-URL families: releases/<tag>/ (only when the
    # probe confirmed the render live) > develop/ (only when the repo
    # HAS a develop branch) > the GitHub page. "stable/" never exists.
    releases_live = bool(c.get("releases_live"))
    if c["latest_vtag"] and releases_live:
        primary_url = "%s/%s/releases/%s/" % (
            SITE_BASE, c["name"], c["latest_vtag"])
    elif latest_ref:
        primary_url = "%s/%s/develop/" % (SITE_BASE, c["name"])
    else:
        primary_url = c["github_url"]
    if esc_desc:
        parts.append(
            '<a href="%s" target="_blank" rel="noopener" '
            'class="block text-base font-semibold leading-snug '
            'text-slate-900 line-clamp-2 hover:text-indigo-700 '
            'hover:underline">%s</a>' % (primary_url, esc_desc))
        parts.append(
            '<a href="%s" target="_blank" rel="noopener" '
            'class="mt-1 block break-all font-mono text-xs %s '
            'hover:text-indigo-700">%s</a>'
            % (c["github_url"], meta_text, esc_name))
    else:
        parts.append(
            '<a href="%s" target="_blank" rel="noopener" '
            'class="block break-all text-base font-semibold '
            'text-slate-900 hover:text-indigo-700 hover:underline">'
            '%s</a>' % (primary_url, esc_name))

    # --- META ROW: taxonomy chips LEFT, content date RIGHT (fixed slot) --
    parts.append(
        '<div class="mt-2 flex flex-wrap items-center gap-2">'
        '<span class="chip uppercase tracking-wide %s">%s</span>%s%s</div>'
        % (CATEGORY_CHIP.get(cat, CATEGORY_CHIP["none"]),
           CATEGORY_LABELS[cat].upper(),
           _status_chip(c["status"], is_muted),
           _date_span(latest_iso,
                      "ml-auto shrink-0 whitespace-nowrap text-[13px] "
                      + meta_text)))

    # --- BODY: topic pastilles as filter toggles (unchanged from v7) -----
    pills = card_topics
    if pills:
        parts.append(
            '<div class="mt-3 flex flex-wrap gap-1.5">%s</div>'
            % "".join(
                '<button type="button" class="chip chip-topic" '
                'data-topic="%s" aria-pressed="false" '
                'title="Filtrer sur le thème : %s">%s</button>'
                % (html.escape(t, quote=True), html.escape(t, quote=True),
                   html.escape(t))
                for t in pills))

    # --- FOOTER (mt-auto, hairline border-top, no fill) ------------------
    # v8.2: footer CHILDREN only - the <footer> wrapper (with its
    # border-t hairline) is emitted only when at least one child exists,
    # so a card with no branches, no actions and no chips has no footer
    # at all (no dangling hairline - HelloGit-class cards).
    foot = []
    # Branches: native <details> disclosure as a full-width subdued
    # text-link + chevron (.dd-link, NOT a button-class element); every
    # row shows its own per-branch content date (absolute visible +
    # data-commit relative rewrite). Omitted entirely when the repo has
    # no disclosed branch (the v6 disabled placeholder is gone).
    if c["branches"]:
        items = "".join(
            '<li><a class="flex min-h-11 items-center gap-3 '
            'whitespace-nowrap px-3 py-3 font-mono text-[13px] '
            'text-slate-700 hover:bg-slate-100 hover:text-indigo-700" '
            'title="rendu éphémère — supprimé avec la branche" '
            'href="%s/%s/previews/%s/" target="_blank" rel="noopener">'
            '<span>%s</span>%s</a></li>'
            % (SITE_BASE, c["name"],
               html.escape(b.replace("/", "-"), quote=True), html.escape(b),
               _date_span(branch_dates.get(b) or c["pushed_at"],
                          "ml-auto shrink-0 text-[13px] text-slate-600"))
            for b in c["branches"])
        foot.append(
            '<details class="dd dd-full"><summary class="dd-link chev">'
            'Branches</summary>'
            '<ul class="dd-list max-h-64 overflow-y-auto py-1">%s</ul>'
            '</details>' % items)
    # Action grid (v8.2 releases gate): Stable (solid primary, BEFORE
    # Latest) links releases/<tag>/ ONLY when the repo has a v* tag
    # whose releases render is probe-confirmed live; grey disabled
    # placeholder (no href) when the tag exists, the render is not live,
    # and develop is; NO Stable affordance when not live and no develop
    # branch. Latest only when the repo has a develop branch (v8.1). One
    # button -> grid-cols-1 (full width), two -> grid-cols-2, none -> no
    # action row. Dates live ONLY in the tooltips (absolute, v8.2).
    btns = []
    if c["latest_vtag"]:
        stable_abs = _abs_date_fr(stable_iso)
        if releases_live:
            btns.append(
                '<a class="btn btn-primary" title="%s" '
                'href="%s/%s/releases/%s/" target="_blank" '
                'rel="noopener">Stable</a>'
                % (html.escape(
                    "Stable · contenu du %s (%s)"
                    % (stable_abs or "date inconnue", c["latest_vtag"]),
                    quote=True), SITE_BASE, c["name"], c["latest_vtag"]))
        elif latest_ref:
            # Tag exists but no live releases/ render: honest disabled
            # placeholder - grey, no href, never a fake-200 link.
            btns.append(
                '<button type="button" class="btn btn-disabled" disabled '
                'title="version stable pas encore publiée">Stable</button>')
    # Latest (.btn outline): ONLY when the repo has a develop branch
    # (v8.1 user ruling — Latest is strictly the develop tip; no
    # develop -> no button). v8.2 ratified spec: the tooltip is the
    # ABSOLUTE French date baked at build time, same grammar as Stable
    # ("Latest · develop, contenu du 19 oct. 2026") - no data-tip-*
    # attributes, no load-time rewrite.
    if latest_ref:
        latest_abs = _abs_date_fr(latest_iso)
        btns.append(
            '<a class="btn" title="%s" href="%s/%s/develop/" '
            'target="_blank" rel="noopener">Latest</a>'
            % (html.escape("Latest · %s, contenu du %s"
                           % (latest_ref, latest_abs or "date inconnue"),
                           quote=True),
               SITE_BASE, c["name"]))
    # Exactly one button -> full-width single column; both -> 1fr/1fr;
    # neither -> no action row at all (footer keeps the Branches
    # disclosure).
    if btns:
        grid_cols = "grid-cols-2" if len(btns) == 2 else "grid-cols-1"
        foot.append('<div class="mt-3 grid %s gap-2">%s</div>'
                    % (grid_cols, "".join(btns)))
    # Conditional chips (zero noise when neither applies).
    chips = ""
    if latest_raw and stable_raw and latest_raw > stable_raw:
        chips += ('<span class="chip chip-new" title="%s">nouveau depuis '
                  'la stable</span>'
                  % html.escape("la version la plus récente est "
                                "postérieure à la version stable",
                                quote=True))
    if latest_iso and generated_at and latest_iso > generated_at:
        chips += ('<span class="chip chip-pending" title="%s">build en '
                  'attente</span>'
                  % html.escape("contenu du %s postérieur à la génération "
                                "du catalogue (%s)"
                                % (latest_iso, generated_at), quote=True))
    if chips:
        foot.append(
            '<div class="mt-2 flex flex-wrap items-center gap-2">%s</div>'
            % chips)
    if foot:
        parts.append(
            '<footer class="mt-auto border-t border-slate-200 pt-3">'
            + "".join(foot) + "</footer>")
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
    # Formation / Filière selects (spec v7 item 3): build-time options
    # derived from the card axes, alphabetical, FR labels via the maps.
    # Filière options carry data-formation (their pairing) so the JS can
    # narrow them when a formation is selected; a filière whose cards
    # have no formation lands in the "Autres" fallback group.
    formations = sorted({c["formation"] for c in cards if c["formation"]})
    formation_opts = '<option value="">Formation…</option>' + "".join(
        '<option value="%s">%s</option>'
        % (html.escape(f, quote=True),
           html.escape(_axis_label(FORMATION_LABELS, f)))
        for f in formations)
    pairs = sorted({(c["formation"], c["filiere"]) for c in cards
                    if c["filiere"]})
    filiere_groups = []
    orphaned = sorted({fi for f, fi in pairs if not f})
    for f in formations:
        fis = sorted(fi for ff, fi in pairs if ff == f)
        if not fis:
            continue
        filiere_groups.append(
            '<optgroup label="%s">%s</optgroup>'
            % (html.escape(_axis_label(FORMATION_LABELS, f), quote=True),
               "".join('<option value="%s" data-formation="%s">%s</option>'
                       % (html.escape(fi, quote=True),
                          html.escape(f, quote=True),
                          html.escape(_axis_label(FILIERE_LABELS, fi)))
                       for fi in fis)))
    if orphaned:
        filiere_groups.append(
            '<optgroup label="Autres">%s</optgroup>'
            % "".join('<option value="%s" data-formation="">%s</option>'
                      % (html.escape(fi, quote=True),
                         html.escape(_axis_label(FILIERE_LABELS, fi)))
                      for fi in orphaned))
    filiere_opts = ('<option value="">Filière…</option>'
                    + "".join(filiere_groups))
    grid = "\n      ".join(_render_card(c, generated_at) for c in cards)
    # Cache-buster for app.css (v6.3): compact UTC stamp of this generation
    # (YYYYMMDDHHMM). Changes every deploy -> new URL -> no stale heuristic
    # cache of the stylesheet (see the <link> comment below).
    css_ver = generated_at[:16].replace("-", "").replace(":", "").replace("T", "")
    return """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>Supports pédagogiques · Emmanuel BRUNO</title>
  <!-- Tailwind v3 purgé, généré au build CI (inputs.css -> public/app.css).
       Aucun CDN : la page est mise en page intégralement même si les
       requêtes tierces sont bloquées et sans FOUC.
       v6.3: le query de version (horodatage UTC de la génération) force
       le rechargement du CSS à chaque déploiement — le serveur applique
       une fraîcheur heuristique (aucun Cache-Control), un app.css daté
       masque les états "pressé" recém-publiés (le HTML, lui, est
       revalidé à chaque navigation). -->
  <link rel="stylesheet" href="app.css?v=%s">
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
          <!-- Formation / Filière (spec v7 item 3): JS-only AND filters
               over data-formation / data-filiere. The filière options
               carry data-formation; the JS hides + disables the
               incompatible ones when a formation is selected. -->
          <select id="f-formation" aria-label="Filtrer par formation"
                  class="h-11 w-full max-w-[12rem] rounded-md border border-slate-300 bg-white px-3 text-sm font-medium text-slate-700">
            %s
          </select>
          <select id="f-filiere" aria-label="Filtrer par filière"
                  class="h-11 w-full max-w-[12rem] rounded-md border border-slate-300 bg-white px-3 text-sm font-medium text-slate-700">
            %s
          </select>
          <label class="flex items-center gap-2 text-sm font-medium text-slate-600">Trier
            <select id="f-sort"
                    class="h-11 rounded-md border border-slate-300 bg-white px-3 text-sm font-medium text-slate-700">
              <option value="">Défaut (catégorie → formation → nom)</option>
              <option value="title-asc">Titre A→Z</option>
              <option value="title-desc">Titre Z→A</option>
              <option value="date-desc">Date récente</option>
              <option value="date-asc">Date ancienne</option>
            </select>
          </label>
          <label class="flex items-center gap-2 text-sm font-medium text-slate-600">Grouper
            <select id="f-group" aria-label="Grouper les résultats"
                    class="h-11 rounded-md border border-slate-300 bg-white px-3 text-sm font-medium text-slate-700">
              <option value="">Aucune</option>
              <option value="f">Formation</option>
              <option value="f-f">Formation + filière</option>
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
    // Dates de contenu : la page est livrée avec la date absolue
    // (juste sans JS, conservée dans l'infobulle title) ; au chargement,
    // le texte est réécrit "il y a X" RE-CALCULÉ depuis l'ISO data-commit
    // à CHAQUE vue — la dérive v4/v5 entre deux déploiements est
    // structurellement impossible ici (rien de relatif n'est cuit au
    // build).
    const grid = document.getElementById('grid');
    const cards = [...grid.querySelectorAll('article')];
    const originalOrder = [...cards];      // ordre de construction (reset du tri)
    const catBtns = [...document.querySelectorAll('#filters [data-cat]')];
    const search = document.getElementById('f-search');
    const countEl = document.getElementById('result-count');
    const emptyEl = document.getElementById('empty');
    const sortSel = document.getElementById('f-sort');
    const topicSel = document.getElementById('f-topic');
    const formationSel = document.getElementById('f-formation');
    const filiereSel = document.getElementById('f-filiere');
    const groupSel = document.getElementById('f-group');
    const afWrap = document.getElementById('active-filters');
    const afChips = document.getElementById('active-filters-chips');
    const total = cards.length;
    let cat = "all";
    const activeTopics = new Set();
    // Axes d'enseignement (spec v7) : filtres AND mono-valeur,
    // indépendants du Set multi de thèmes.
    let formation = "";
    let filiere = "";

    // Dates de contenu : "il y a X" recalculé à chaque vue depuis l'ISO
    // (ruling 1 — jamais périmé entre deux builds cron ; sans JS, l'absolu
    // visible reste exact).
    function relFr(iso) {
      const s = (Date.now() - new Date(iso).getTime()) / 1000;
      if (s < 3600) return "il y a " + Math.max(1, Math.floor(s / 60)) + " min";
      const d = Math.floor(s / 86400);
      if (d < 1) return "il y a " + Math.floor(s / 3600) + " h";
      if (d < 7) return "il y a " + d + (d > 1 ? " jours" : " jour");
      if (d < 28) { const w = Math.floor(d / 7); return "il y a " + w + (w > 1 ? " semaines" : " semaine"); }
      if (d < 365) return "il y a " + Math.max(1, Math.floor(d / 30)) + " mois";
      const a = Math.floor(d / 365);
      return "il y a " + a + (a > 1 ? " ans" : " an");
    }
    document.querySelectorAll('[data-commit]').forEach(el => {
      el.textContent = relFr(el.dataset.commit);
      el.setAttribute("aria-label", el.textContent);
    });
    // Repli tactile (v8.2) : un tap sur le <time> (slot méta, hors lien)
    // bascule relatif <-> absolu (data-abs embarqué au build) ; le
    // re-tap restaure la forme relative. Les lignes de branche sont des
    // liens : elles gardent le clic de navigation, pas de bascule.
    document.querySelectorAll('time[data-abs]').forEach(el => {
      if (el.closest("a")) return;
      el.style.cursor = "pointer";
      el.addEventListener("click", () => {
        const pinned = el.dataset.pinned === "1";
        el.dataset.pinned = pinned ? "" : "1";
        const txt = pinned ? relFr(el.dataset.commit) : el.dataset.abs;
        el.textContent = txt;
        el.setAttribute("aria-label", txt);
      });
    });

    // Filtre AND : catégorie x recherche x intersection des thèmes
    // x formation x filière (spec v7 item 3).
    function applyFilters() {
      const q = (search.value || "").trim().toLowerCase();
      const sel = [...activeTopics];
      let visible = 0;
      cards.forEach(c => {
        const topics = (c.dataset.topics || "").split(" ");
        const ok = (cat === "all" || c.dataset.area === cat)
          && (!q || (c.dataset.search || "").includes(q))
          && sel.every(t => topics.includes(t))
          && (!formation || (c.dataset.formation || "") === formation)
          && (!filiere || (c.dataset.filiere || "") === filiere);
        c.classList.toggle("hidden", !ok);
        if (ok) visible++;
      });
      countEl.textContent = visible + "/" + total;
      emptyEl.classList.toggle("hidden", visible !== 0);
      applyGroups();
    }

    // Libellé d'un axe : textContent de l'option correspondante du
    // select (le select est la source des libellés FR), slug en repli.
    function axisLabel(sel, val) {
      const o = [...sel.options].find(x => x.value === val);
      return o ? o.textContent : val;
    }

    // Narrowing (spec v7 item 3) : formation choisie -> options de
    // filière incompatibles masquées + désactivées, sélection
    // éventuellement incompatible effacée.
    function narrowFiliere() {
      const f = formationSel.value;
      [...filiereSel.options].forEach(o => {
        if (!o.value) return;            // le placeholder reste actif
        const ok = !f || o.dataset.formation === f;
        o.hidden = !ok;
        o.disabled = !ok;
      });
      if (filiere && formation &&
          ![...filiereSel.options].some(o => o.value === filiere && !o.disabled)) {
        filiere = "";
        filiereSel.value = "";
      }
    }

    // Regroupement (spec v7 item 4) : en-têtes .group-h devant la
    // première carte VISIBLE de chaque groupe, dans l'ordre courant du
    // DOM. Cartes sans formation jamais groupées. Nœuds construits à la
    // main (jamais innerHTML) ; les en-têtes sont TOUJOURS retirés en
    // début de passe (aucun résiduel après changement de filtre/mode).
    function applyGroups() {
      grid.querySelectorAll(".group-h").forEach(h => h.remove());
      const mode = groupSel.value;
      if (!mode) return;
      let prev = "\u0000";
      [...grid.children].forEach(el => {
        if (el.tagName !== "ARTICLE" || el.classList.contains("hidden")) return;
        const f = el.dataset.formation || "";
        if (!f) return;
        const fi = mode === "f-f" ? (el.dataset.filiere || "") : "";
        const key = f + "|" + fi;
        if (key === prev) return;
        prev = key;
        const h = document.createElement("div");
        h.className = "group-h col-span-full";
        h.setAttribute("role", "heading");
        h.setAttribute("aria-level", "2");
        h.textContent = fi
          ? axisLabel(formationSel, f) + " \u2014 " + axisLabel(filiereSel, fi)
          : axisLabel(formationSel, f);
        grid.insertBefore(h, el);
      });
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
      if (formation) {
        afChips.appendChild(xChip(axisLabel(formationSel, formation),
          "Retirer la formation", () => {
            formation = "";
            formationSel.value = "";
            narrowFiliere();
            update();
          }));
      }
      if (filiere) {
        afChips.appendChild(xChip(axisLabel(filiereSel, filiere),
          "Retirer la filière", () => {
            filiere = "";
            filiereSel.value = "";
            update();
          }));
      }
      afWrap.classList.toggle("hidden",
        cat === "all" && !q && activeTopics.size === 0
        && !formation && !filiere);
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
    // Formation / Filière (spec v7) : changements AND + narrowing ;
    // Grouper rejoue filtres + en-têtes (aucune puce, ce n'est pas un filtre).
    formationSel.addEventListener("change", () => {
      formation = formationSel.value;
      narrowFiliere();
      update();
    });
    filiereSel.addEventListener("change", () => {
      filiere = filiereSel.value;
      update();
    });
    groupSel.addEventListener("change", applyFilters);
    function clearFilters() {
      search.value = "";
      activeTopics.clear();
      formation = "";
      filiere = "";
      formationSel.value = "";
      filiereSel.value = "";
      narrowFiliere();
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
 """ % (css_ver, "\n          ".join(chip_btns), topic_opts,
        formation_opts, filiere_opts, count, count,
        grid, _gen_date_fr(generated_at), count)




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
            "formation": _axis_of(r.get("topics"), "formation-"),
            "filiere": _axis_of(r.get("topics"), "filiere-"),
            "pushed_at": r.get("pushed_at"),
            "github_url": r.get("html_url")
                          or "https://github.com/%s/%s" % (ORG, r["name"]),
        })

    # Per-repo branches + v* tags + content dates for the v8 card
    # anatomy (ThreadPool(8), 2 base + ≤14 commits REST calls per repo at
    # the global 5/s pacing; date failures fall back to pushed_at).
    extras_started = time.time()
    extras = fetch_extras([(c["name"], c["default_branch"]) for c in cards])
    for c in cards:
        branches, latest_vtag, dates = extras.get(c["name"], ([], None, {}))
        c["branches"] = branches
        c["latest_vtag"] = latest_vtag
        c["dates"] = dates
    print("Branches/tags/dates: %d repos in %.1fs" %
          (len(cards), time.time() - extras_started))

    # v8.2 releases/ liveness gate: one plain public GET per tagged repo
    # (deduped), keyed on the body signature - never the 200 status (the
    # host catch-all). Dead/unknown probes demote the Stable link to the
    # honest placeholder and the primary URL to develop/GitHub.
    probe_started = time.time()
    probed, live_n = probe_releases(cards)
    print("Releases probe: %d candidates, %d live in %.1fs" %
          (probed, live_n, time.time() - probe_started))

    # Deterministic default order (spec v6.2 item 6, refined v7 item 5):
    # full-taxonomy category rank, then the baked data-sortkey (lecture
    # cards: formation -> filière -> name; every other card: name), raw
    # name as tie-breaker. The JS "Défaut" option and the originalOrder
    # snapshot reuse this DOM order as-is.
    cards.sort(key=lambda c: (
        CATEGORY_RANK.get(c["category"], CATEGORY_RANK["none"]),
        _default_sortkey(c), c["name"]))

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
