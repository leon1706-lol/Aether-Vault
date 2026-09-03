# Policy examples

Three worked `.av/policies.json` shapes for `av promote`/`av merge`'s policy gate
(`av policy set` writes this same shape; these files are drop-in equivalents you can copy
directly into a repo's `.av/policies.json`, or use as `--baseline-ref`/`--threshold`
reference when calling `av policy set` by hand):

- `metric-gate.json` — deny promotion unless `val_loss` improves against the branch's
  previous tip (`baseline_ref: "main~1"`). Equivalent CLI:
  `av policy set main val_loss "<" --baseline-ref main~1`
- `signature-gate.json` — deny promotion of any candidate without a valid embedded
  signature (`av registry keygen` first). No metric involved. Equivalent CLI:
  `av policy set main --require-signature`
- `combined-gate.json` — both gates on the same branch: signed AND `val_loss < 0.5`
  (absolute threshold, not baseline-relative). Equivalent CLI:
  `av policy set main val_loss "<" --threshold 0.5 --require-signature`

`tests/test_v120.py::test_example_policies_load_and_evaluate` loads each of these files
for real and exercises `av_cli.cmd_policy.evaluate()`/`candidate_is_signed()` against
them, so they can't silently drift out of sync with the schema `av policy set` actually
writes.
