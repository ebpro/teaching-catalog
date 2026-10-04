# ebpro teaching catalog

Auto-generated **trunk-based readiness board** for every `notebook-*` and `lecture-*`
repo in the [ebpro](https://github.com/ebpro) org. For each repo it shows the last
stable tag, the current trunk (default-branch) head, and every other branch ranked
by divergence (`ahead`/`behind`).

- **Live:** <https://teaching-catalog.pages.dev> — auto-refreshes every 6 hours via
  GitHub Actions (`.github/workflows/catalog.yml`), plus on-demand via
  `workflow_dispatch`. Deployed to Cloudflare Pages (production).
- Pure Python 3 stdlib, no third-party deps. The generated `public/` is not committed.
- To change which repos are listed, edit the `PREFIXES` constant at the top of `catalog.py`.
