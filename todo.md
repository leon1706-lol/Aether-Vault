# To-Do — Objectives Canvas

This is the owner's planning space, not a generated backlog. Whatever is written below is
the current objective(s) and any personal notes/context for it — read it before starting
work in this repo, and treat it as the live brief for what an AI agent should do next.
Expect this file to be rewritten or cleared out entirely as objectives change; it does not
accumulate history (that's what `development/CHANGELOG.md` and `development/Probleme.md`
are for — see `AGENTS.md`).

---


Main Objektive V1.3.1
Feature list to move from “autonomous training substrate” → “RSI-capable platform”
Below is a practical backlog. Aether-Vault already covers memory, lineage, runs, env replay, semantic diff, events, and basic promote gates. Everything here is what is still missing for serious RSI.

A. Outer-loop / meta-improvement (the actual RSI core)
    1. Meta-run type
Runs that improve the improver (agent code, prompts, tools, search policy), not only the target model. 
    2. Improver artifact versioning
First-class versioning of agent code, tool schemas, prompts, and policy packs as their own trinity (or linked commits). 
    3. Self-edit proposals
Structured “change sets” the agent proposes to its own stack (diff + rationale + predicted risk). 
    4. Self-edit application + rollback
Apply improver changes in isolated sandboxes; one-command rollback to last known-good improver. 
    5. Improver lineage graph
Explicit parent links: which improver version produced which model runs and which later improver versions. 

B. Open-ended objectives & curriculum
    6. Task / eval registry
Versioned eval suites with frozen snapshots (content-addressed, like models). 
    7. Eval immutability locks
Policy: training runs may not modify the eval definitions they are scored against. 
    8. Curriculum engine hooks
API for proposing new tasks, difficulty ramps, and held-out probes. 
    9. Capability canaries
Small fixed tests that must not regress when the improver or model changes. 
    10. Metric gaming detection signals
Track train/eval gaps, eval-only hacks, suspicious data overlap; surface them in run summaries. 

C. Safe self-modification control
    11. Dual-gate promotion
Separate gates: (a) promote target model, (b) promote improver/agent code. 
    12. Privilege levels
Agent can edit model training code freely; editing promotion rules / evals / tools requires higher privilege or human approval. 
    13. Policy-as-code (signed)
Promotion and self-edit rules stored as signed, versioned objects; changing them is itself an auditable run. 
    14. Mandatory canary pass before improver promote
Improver changes blocked unless canaries + smoke suite pass. 
    15. Kill-switch / freeze mode
Global pause: no promotes, no self-edits, only read + rollback. 

D. Experiment design & research policy
    16. Experiment planner interface
Agent outputs a structured plan (hypotheses, ablations, budget, stop rules) stored on the run. 
    17. Budget accounts
Per-run / per-lineage compute & storage quotas; hard stop when exhausted. 
    18. Branch exploration policy
Rules for when to branch, merge, or abandon lines (tied to runs + metrics). 
    19. Auto-stop conditions
Plateau detection, divergence, NaNs, canary failure → run marked failed and resources released. 
    20. Parallel bandit / scheduler hooks
Events + API so an external scheduler can start/stop runs based on live metrics. 

E. Credit assignment & long-horizon memory
    21. Causal run graphs
Not only parent_run_id: explicit links “this code change caused that metric delta” (agent-authored + optional verified). 
    22. Strategy memory
Searchable store of what worked/failed across lineages (beyond .avh notes): techniques, hyperparameters, data mixes. 
    23. Distilled lessons object
Compact, versioned “what we believe now” document the next agent must read before starting a run. 
    24. Cross-run semantic search
Query: “all runs where eval_acc rose after LR schedule change.” 

F. Stronger evaluation integrity
    25. Held-out eval vault
Separate project/repo the training agent cannot write to; only a scorer role can append results. 
    26. Blind / delayed scoring
Optional: agent sees training metrics live, final held-out score only after freeze. 
    27. External eval adapters
Hooks to third-party benchmarks so the system can’t silently redefine success. 
    28. Reproducible scoring runs
Scoring itself is a run type with env snapshot + code pin. 

G. Runtime isolation & execution
    29. Sandbox executor
Training and self-edits run in isolated environments (containers) with declared mounts and network policy. 
    30. Tool permission manifests
Per-improver-version allowlist of tools (file write paths, network, GPU). 
    31. Deterministic replay of agent actions
Action log + env snapshot → replay agent decisions, not only training code. 
    32. Resource controller integration
Native hooks to Kubernetes/Slurm/local GPU pool (queue, preempt, cancel by run_id). 

H. Multi-agent RSI
    33. Role-separated agents
Researcher / Trainer / Reviewer / Promoter as different identities with different auth scopes. 
    34. Reviewer gate
Another agent (or human) must approve improver promotes; recorded as audit + run event. 
    35. Debate / critique objects
Structured objections attached to a proposed promote; must be resolved or waived. 
    36. Shared blackboard
Beyond events: durable shared state for hypotheses and claims with authors and evidence links. 

I. Safety & monitoring
    37. Continuous regression dashboard
Canaries, eval trends, improver churn, failed self-edits. 
    38. Anomaly alerts
Sudden metric jumps, mass file rewrites, policy changes, auth spikes → webhook/event. 
    39. Tamper-evident policy log
Append-only history of every rule change (build on audit + signed commits). 
    40. Incident rollback playbooks
One command: freeze + restore last good improver + last good model + last good policy pack. 

J. Product surfaces (so the above is usable)
    41. av meta-run / improver CLI + SDK 
    42. av canary / av eval commands 
    43. av budget and scheduler API 
    44. WebUI: Improver lineage + canary status + pending self-edits 
    45. Docs: “RSI operator guide” (human + agent) 
    46. Reference loop implementation (one end-to-end open-source agent that uses all of the above) 

Suggested build order (realistic)
Phase R1 — Make self-change explicit

Meta-runs, improver artifacts, dual-gate promote, canaries, freeze/rollback.
Phase R2 — Protect the objective

Frozen eval vault, scoring runs, gaming signals, capability canaries.
Phase R3 — Research control

Budgets, planner objects, auto-stop, scheduler hooks.
Phase R4 — Multi-agent + strategy memory

Roles, reviewer gate, strategy store, cross-run search.
Phase R5 — Hard isolation

Sandbox executor, tool manifests, action replay.

What you should not try to own
    • The foundation model weights/training from scratch 
    • A full AGI cognitive architecture 
    • Cluster OS / full K8s product 
Stay the RSI control plane: memory, lineage, gates, eval integrity, improver versioning, audit.

One-sentence summary
To become RSI-capable, add versioned self-modification of the improver, immutable evals + canaries, dual promotion gates, budgets/schedulers, strategy memory, and sandbox isolation — on top of the training-loop substrate you already built.
