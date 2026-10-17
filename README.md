# ebpro teaching catalog

Auto-generated **card-grid catalog** of the ebpro teaching surface, organised in
**three categories only**, assigned by repo **name prefix**: `lecture-` →
Lecture, `notebook-` → Notebook, `sample-` → Samples (plus the forced names
`ConflictBot`, `HelloGit`). Archived repos, repos carrying the `status-legacy`
topic and non-matching names are excluded. Cards use the v5 sober mobile-first
layout: a neutral white surface (the category is a label in the meta row — no
more per-category pastel card backgrounds; colour is reserved for
actions/status), the description as the primary line (2-line clamp), the repo
name as a small monospace secondary line, a colour-coded AA status chip (dark
text on a light tint; `frozen`/`duplicate` get a muted slate pastille instead
of card-level opacity) and verbatim GitHub topic pastilles (`area-*` omitted),
and a 44px-tap-target links footer: `Stable` (solid accent, only when the repo
has a `v*` tag; points at the canonical `stable/` pointer path) BEFORE
`Latest` (the develop render), `Source` (public repos only) and a `Branches`
native `<details>` disclosure of the non-default branches (each linking a
`previews/<branch>/` path; works with JS blocked). Filters: sticky slim header
with live search; the filter panel collapses behind a "Filtres" toggle below
`lg` and is inline from `lg` up; a category segmented control (`aria-pressed`),
a topic checkbox dropdown (AND semantics) and a result count; last-update
dates render as absolute short French dates at build time, rewritten to
"il y a X" by JS when available. Plain vanilla JS.

- **Live:** <https://bruno.univ-tln.fr/teaching-material/catalog/> —
  regenerated every 6 hours via GitHub Actions
  (`.github/workflows/catalog.yml`: `build` job on hosted ubuntu-latest
  (Python 3.12 + Node 22) runs `python catalog.py` and then builds the purged
  CSS; `deploy` job on a self-hosted `ebpro-org` runner rsyncs `public/`
  (`--delete` scoped to `catalog/`) to the teaching server
  (`vai.univ-tln.fr`, served under `bruno.univ-tln.fr/teaching-material/`) and
  drops a root redirect `index.html`), plus on-demand via
  `workflow_dispatch`. The generated `public/` is not committed.
- **Architecture (v5.1):** a pure Python 3 stdlib generator (no third-party
  deps) emits a static `public/index.html`; styling is a **build-time purged
  Tailwind v3.4.19** stylesheet — `tailwind.config.js` + `inputs.css` →
  `public/app.css` via the CI step
  `npx tailwindcss@3.4.19 -c tailwind.config.js -i inputs.css -o public/app.css --minify`,
  whose single content source is `catalog.py` itself (every class literal
  lives in its HTML templates). No runtime CDN and no webfont requests: the
  page links only its local stylesheet and uses the system font stack.
  daisyUI was removed in v5 (its pre-built `full.min.css` cannot be purged
  and its CDN delivery was a blocklist/FOUC risk); the card/chip/button/
  disclosure components are hand-rolled in `inputs.css` on top of the default
  Tailwind palette (slate surfaces, single indigo accent), light-only
  (`darkMode: "class"`), with visible keyboard focus, a skip link and
  `prefers-reduced-motion` support. Locally: `GITHUB_TOKEN=… python
  catalog.py` regenerates `index.html`; run the `npx` command above to
  regenerate `app.css`.
- **Fleet freshness:** whenever a course site finishes rendering, the shared
  `_quarto-utils` reusable workflow (`render-quarto.yml`) runs its
  `trigger-catalog` job, which dispatches a `catalog.yml` rebuild of this
  repo on `main` (needs `CATALOG_DISPATCH_TOKEN` with Actions:write here;
  warn-only — the 6 h cron is the fallback).
- The `cleanup` subcommand (`python catalog.py cleanup [--dry-run] [--keep N]`)
  prunes old Cloudflare Pages preview deployments (needs `CF_API_TOKEN`) and
  is driven by `.github/workflows/cleanup.yml` (weekly, Sunday 04:00 UTC).
- To change which repos are listed, adjust the category rules
  (`CATEGORIES` / `FORCED_SAMPLES` / `_classify`) at the top of `catalog.py`.
  Category assignment is by name prefix on purpose (robust to topic-labelling
  drift); `status-legacy` and `archived` remain topic/flag-driven.
