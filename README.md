# ebpro teaching catalog

Auto-generated **card-grid catalog** of the ebpro teaching surface, organised in
**three categories only**: Notebook (repos tagged `fmt-ipynb`, which wins over
the area topic), Lecture (`area-lecture`, non-ipynb) and Samples
(`area-sample` plus the forced names `ConflictBot`, `eye`, `HelloGit`). Every
other repo (archived, `area-standard` / `reference` / `student` / infra / cicd /
image / meta / personal, untagged) is excluded. Cards use a distinct pastel
background per category and a pill-button footer: `Site · Develop`, `Site ·
Stable` (only when the repo has a `v*` tag; points at the canonical `stable/`
pointer path), `Source` (GitHub) and a zero-JS `Branches ▾` dropdown of the
non-default branches (each linking a `previews/<branch>/` path).

- **Live:** <https://bruno.univ-tln.fr/teaching-material/catalog/> —
  regenerated every 6 hours via GitHub Actions
  (`.github/workflows/catalog.yml`: build on hosted ubuntu-latest, deploy job
  rsyncs `public/` to the teaching server), plus on-demand via
  `workflow_dispatch`.
- Pure Python 3 stdlib, no third-party deps. Tailwind CSS is loaded from the
  Play CDN in the generated page (v1; swap to build-time compiled CSS later).
  The generated `public/` is not committed.
- The `cleanup` subcommand (`python catalog.py cleanup [--dry-run] [--keep N]`)
  prunes old Cloudflare Pages preview deployments and is driven by
  `.github/workflows/cleanup.yml`.
- To change which repos are listed, adjust the category rules
  (`CATEGORIES` / `FORCED_SAMPLES` / `_classify`) at the top of `catalog.py` —
  the source of truth for the topics is the org labeling (`area-*` /
  `status-*` / `fmt-ipynb` topics).
