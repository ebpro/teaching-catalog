# ebpro teaching catalog

Auto-generated **card-grid catalog** of the ebpro teaching surface, organised in
**three categories only**, assigned by repo **name prefix**: `lecture-` →
Lecture, `notebook-` → Notebook, `sample-` → Samples (plus the forced names
`ConflictBot`, `HelloGit`). Archived repos, repos carrying the `status-legacy`
topic and non-matching names are excluded. Cards use a distinct pastel
background per category (the colour **is** the category signal; badges show
the raw GitHub topics verbatim), the description as the primary line and the
repo name as a monospace secondary line, and a daisyUI button footer: `Site ·
Develop`, `Site · Stable` (only when the repo has a `v*` tag; points at the
canonical `stable/` pointer path), `Source` (public repos only) and a `⎇
Branches ▾` hover dropdown of the non-default branches (each linking a
`previews/<branch>/` path). Filters: a category join-group plus a collapsible
topic multi-select (AND semantics), plain vanilla JS.

- **Live:** <https://bruno.univ-tln.fr/teaching-material/catalog/> —
  regenerated every 6 hours via GitHub Actions
  (`.github/workflows/catalog.yml`: build on hosted ubuntu-latest, deploy job
  rsyncs `public/` to the teaching server), plus on-demand via
  `workflow_dispatch`.
- Pure Python 3 stdlib, no third-party deps. Tailwind CSS is loaded from the
  Play CDN and daisyUI 4 from a pinned cssdelivr stylesheet (v1; swap to
  build-time compiled CSS later).
  The generated `public/` is not committed.
- The `cleanup` subcommand (`python catalog.py cleanup [--dry-run] [--keep N]`)
  prunes old Cloudflare Pages preview deployments and is driven by
  `.github/workflows/cleanup.yml`.
- To change which repos are listed, adjust the category rules
  (`CATEGORIES` / `FORCED_SAMPLES` / `_classify`) at the top of `catalog.py`.
  Category assignment is by name prefix on purpose (robust to topic-labelling
  drift); `status-legacy` and `archived` remain topic/flag-driven.
