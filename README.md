# ebpro teaching catalog

Auto-generated **card-grid catalog** of the ebpro teaching surface: every org
repo tagged `area-lecture` / `area-sample` / `area-standard` / `area-reference`
/ `area-student` (plus anything tagged `fmt-ipynb`), rendered as pastel Tailwind
cards with area/status chips, a site link for render-caller areas, the GitHub
source link and a relative "last updated" date. Archived repos and the
non-teaching areas (infra / cicd / image / meta / personal) are excluded.

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
- To change which repos are listed, adjust the area constants
  (`INCLUDE_AREAS` / `EXCLUDE_AREAS`) at the top of `catalog.py` — the source
  of truth for the topics is the org labeling (`area-*` / `status-*` topics).
