# Recall CI workflows (staged, not yet active)

These two GitHub Actions workflow files belong in `.github/workflows/` to enable
the auto-quality-gate + auto-README-update flow described in the top-level
README's "Retrieval quality" section.

They live here (not in `.github/workflows/`) because pushing to that path
requires the GitHub OAuth `workflow` scope, which the maintainer's token
doesn't currently have. The plan was to keep this work-in-progress visible
without forcing a token-scope refresh under the wrong account.

## Activate

```bash
gh auth refresh -h github.com -s workflow      # one-time scope grant
mv tests/recall/ci_workflows/recall-bench.yml          .github/workflows/
mv tests/recall/ci_workflows/recall-bench-update.yml   .github/workflows/
git add .github tests/recall/ci_workflows
git commit -m "ci(recall): activate quality gate + README update workflows"
git push
```

## What they do, briefly

- **`recall-bench.yml`** — runs on every PR that touches `recall/`. Executes
  `bench_e2e.py` at scale 80, compares to `bench_baseline.json`. Fails the
  PR if hybrid bucket-paraphrase recall@5 drops by more than 5pp. Posts a
  comment on the PR with the comparison table.

- **`recall-bench-update.yml`** — runs on push to `main` after `recall/`
  changes. Re-benches at scales 80/1000/5000, refreshes
  `tests/recall/bench_baseline.json` and the README's "Retrieval quality"
  section, commits the result back to `main`.

## Manual fallback (until activated)

```bash
python tests/recall/bench_e2e.py --report --scale 80 --json /tmp/cur.json
python tests/recall/bench_compare.py \
    --current /tmp/cur.json \
    --baseline tests/recall/bench_baseline.json \
    --tolerance 5
```

Exit 0 = quality preserved, exit 1 = regression past tolerance. Run this
locally before opening any PR that touches `recall/` until CI is live.
