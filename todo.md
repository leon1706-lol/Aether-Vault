
Main Objektive V1.2.5
- continuation of the gapfilling of V1.2 features
1. Engine image consolidation
    • Cleaner process supervision (signal handling, graceful drain, independent restart of webui vs server without killing the whole container) 
    • Documented upgrade path when legacy aliases are removed 
    • Stronger production health semantics (readiness vs liveness) 
2. Env snapshot & replay
    • Deeper execute path (venv/conda target, not only current interpreter pip install) 
    • Capture more training-relevant state (CUDA toolkit version, GPU name, critical env vars list, optional conda env name) 
    • Real validation mode that checks “can this recipe resolve?” without installing 
    • Higher-quality Dockerfile (multi-stage, CUDA base options, non-root) 
    • Golden fixtures proving bit-identical snapshot IDs across machines/OS 
3. Dataset CDC generalization
    • Broader default set (e.g. common dataset dumps / arrow-related patterns where safe) 
    • Clearer docs + examples for .avattributes 
    • Explicit opt-in for formats that are risky to chunk by default 
    • semdiff always reports realized chunk-reuse / dedup efficiency in a stable machine field used by .avh 
4. Audit log depth
    • Richer filters (actor, outcome, route family) + stable pagination tokens 
    • Export format for compliance (CSV/JSONL) 
    • Guaranteed coverage matrix test: every mutating route appears in audit 
    • Clear retention + prune UX in CLI (av audit prune or documented admin-only) 
5. Signed commits
    • Key management UX (list keys, show fingerprint, rotate with guidance) 
    • Optional signature requirement policy on protected branches 
    • Stronger canonicalization tests across clone/pull/server round-trips 
    • Explicit “not a PKI / not identity binding” callouts in CLI help, not only SECURITY.md 
    • Optional detached signature export for external audit 
6. WebUI run detail
    • Dedicated run detail view (not only expandable list row) 
    • Show context-memory notes from .avh / handoff 
    • Metrics history chart or table over the run’s commits 
    • Server-side semantic summary (not only client-side from last two trees) 
    • Deep-linkable run URL + loading/error empty states polished 
7. Plugin migration
    • Plugins call the same functions as av_sdk.Repo with zero remaining CLI/chdir assumptions 
    • Perfect parity tests: plugin commit ≡ SDK commit ≡ CLI commit (hash, metrics, run linkage) 
    • Documented extension guide for new frameworks on the SDK path only 
8. Webhook delivery maturity
    • Persistent dead-letter queue with inspect/replay CLI or admin API 
    • Delivery attempt history visible per webhook 
    • Better observability (last success/failure, consecutive failures, disable-after-N) 
    • Poison-message isolation and max-attempt policy fully documented + tested 
9. Perf regression gates
    • Stable probes that don’t flake on disk-heavy paths 
    • Separate budgets for commit / status / log / semdiff 
    • CI fails only on real regressions, not noise 
10. Multi-agent conflict UX
    • Every divergence/conflict message includes run IDs when present 
    • Exact copy-paste remediation commands in JSON + human modes 
    • Consistent behavior on pull/merge/push races 

