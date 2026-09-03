# `.avattributes`

Per-path staging directives — like `.gitattributes`, but for how Aether-Vault stores a
file's *content*, not its line endings. One glob pattern per line, matched
repo-relative; the **last matching line wins** (a later line's flags fully replace an
earlier one's for the same path — flags never merge across lines).

```
<glob-pattern> <flag> [<flag> ...]
```

Generate a starter file: `av file --avattributes`.

## Flags

| Flag | Effect |
|---|---|
| `no-chunk` | Store as one whole-file blob instead of content-defined chunks. Applies above the LFS threshold, to files whose extension is in the default chunkable set (below) or that opted in with `chunk`. |
| `chunk` | Force-enable content-defined chunking for an extension that wouldn't otherwise qualify. `no-chunk` on the same matching line always wins over `chunk` — safety first. |
| `no-layer-split` | Never split a `.safetensors` file into per-layer shards; store it whole. |

An unknown flag on a matching line is silently ignored (forward compatibility — a file
written for a newer CLI version doesn't break an older one).

## The default chunkable set, and why "risky formats" are opt-in

Content-defined chunking (CDC) only pays off when a file's edits are genuinely
*localized* — a training checkpoint that overwrites a few tensors, an append-only log.
The **default chunkable extensions** (`python/av_cli/core.py::CHUNKABLE_EXTS`) are
formats where that's usually true:

```
.pt .pth .ckpt .npz .h5 .hdf5 .pb .msgpack .bin .onnx .model .arrow .feather .pkl .pickle
```

**Compressed or columnar containers are deliberately excluded from this list, by
design — not an oversight.** `.parquet` (per-column compression), `.zip`, `.gz`, `.tar`,
`.7z`, and similar formats typically rewrite their *entire byte stream* on any logical
edit (recompression shifts every byte after the change point), so content-defined chunk
boundaries almost never survive between versions — you'd pay the CDC overhead and get
none of the deduplication benefit. These formats stay whole-file (LFS-style) unless you
explicitly opt them in with `chunk`, and only after confirming your specific export
pipeline is actually append-only or otherwise chunk-friendly (e.g. an uncompressed,
block-aligned dump).

This is enforced structurally, not just documented: `tests/test_dataset_cdc.py`'s
matrix asserts every default-chunkable extension actually gets chunked and every
excluded one doesn't unless `chunk` is given, and `no-chunk` always overrides `chunk` on
a shared line.

## Worked examples

```
*.pt no-chunk                          # never chunk plain PyTorch state dicts
models/frozen/** no-chunk no-layer-split   # frozen release models: no chunking, no layer split
experiments/*.safetensors no-layer-split   # keep chunking (safetensors aren't chunked anyway),
                                            # just skip the per-layer shard split
datasets/exports/*.parquet chunk       # opted in: this export pipeline only appends rows
raw/*.wav chunk                        # uncompressed audio, block-aligned — safe to chunk
archives/*.zip                         # deliberately NOT opted in — recompresses on any edit
scratch/** no-chunk no-layer-split      # last-line-wins: blanket override for a whole tree
```

## Where this shows up

- `av add` reads `.avattributes` once per invocation and applies it inside
  `stage_one_file()` — the same staging path `av commit`, `av watch`, and every plugin
  callback ultimately funnel through, so the directives apply uniformly regardless of
  which surface staged the file.
- `av diff` / `.avh`'s `semantic_summary.chunks` reports realized dedup (count- and, since
  v1.3.0, byte-weighted) for whatever ended up chunked — see `docs/contracts.md`'s
  `semdiff-1.0` schema.
