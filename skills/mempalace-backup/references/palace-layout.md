# Palace layout & knowledge graph selection

A local MemPalace has a **HOME** dir (default `~/.mempalace`) and a **DATA**
dir (default `HOME/palace`). Keep the two distinct when backing up or importing.

```
~/.mempalace/                         <- HOME  (config/home dir; pass as --palace)
├── config.json                       <- palace_path -> ~/.mempalace/palace
├── knowledge_graph.sqlite3           <- optional historical KG
├── tunnels.json
├── wal/  locks/
└── palace/                           <- DATA
    ├── chroma.sqlite3                <- vectors + drawer metadata
    ├── knowledge_graph.sqlite3       <- DATA-local KG
    ├── sqlite_exact.sqlite3          <- SQLite-exact drawers
    ├── logstream.sqlite3             <- task events AND artifacts/link tables
    ├── replica.json                  <- stable log provenance, not restore epoch
    ├── <uuid>/*.bin                  <- HNSW index
    └── .mempalace/origin.json        <- embedder identity (critical)
```

## DATA and physical backups

DATA/logstream.sqlite3's `events`, `artifacts`, and `event_artifacts` tables hold
all indispensable task journal/source content. WAL/SHM files beside it can
contain committed pages.
There is no independent artifact directory or task pending/head/clock database
that must match the restored snapshot.

The backup helper keeps `--palace` as HOME, accepts global `--data-path`, and
passes DATA to the installed MemPalace CLI's own `--palace` option. The latter
is not HOME. MemPalace 3.12 resolves environment variables in the order
`MEMPALACE_PALACE_PATH`, `MEMPALACE_PALACE`, `MEMPAL_PALACE_PATH`, then config;
3.8 ignores the middle alias. Use the same interpreter as the server. If its
runtime version is unknown and the middle alias is set, provide `--data-path`.
Configured DATA outside HOME or in a linked/control subtree is refused.
Physical inventory and checkpointing cover both HOME and DATA KGs when present,
including their WAL/SHM files; task-safe backup and restore writer exclusions
also cover both. Both task and memory-only captures record DATA in
`.palace-backup.json`, so restores do not use old live config or environment.

The current user's historical state KG can lie outside these covered KG
locations when HOME is customized. If it may be the server's active graph, the
backup and in-place restore helpers refuse. `backup --kg-path` and
`restore --kg-path` can assert a known active HOME/DATA graph; they cannot add
an uncovered graph to a physical snapshot.

The canonical writer lease is always under the **current user's**
`~/.mempalace/locks/mine_palace_<16-hex-canonical-DATA-hash>.lock`, even for a
different backup HOME. Keep this inode and namespace continuously in place.
Publication moves content entries, not HOME or its `locks/`/`server/` controls.

## Logical wing export/import

- `--palace` is the **HOME** dir (`~/.mempalace`), never the nested
  `~/.mempalace/palace` DB dir.
- `--data-path` overrides the environment and HOME config for DATA. The helper
  binds `MEMPALACE_PALACE_PATH` to DATA, not HOME, for MemPalace operations.
- With MemPalace 3.12, the DATA-local KG wins when it contains triples or
  entities, including when the state graph also has rows. For the default
  palace, an absent or empty DATA KG falls back to a populated historical
  current-user state KG. Unreadable graphs are not treated as empty.
- With custom DATA and a populated historical graph but absent or empty DATA KG,
  launch mode can change the selected graph. MemPalace 3.8 can also choose
  differently even when DATA has rows. The helper refuses to guess in these
  cases or for unrecognized runtimes with an existing KG file or KG writes. Determine the
  server's target and pass `--kg-path` pointing to its DATA-local, HOME-level,
  or current-user state KG. Explicit selection does not merge or delete graphs.
- Export and import resolve the KG once; import preflight and direct KG writes
  use that same path.
- The import **preflight** aborts when the resolved Chroma DB does not exist
  unless `--create-new-palace`. This guards against the common mistake of
  pointing `--palace` at the DATA dir and creating a stray Chroma DB.

## mempalace quirk: brand-new palaces

When **no** Chroma DB exists at `palace_path`, mempalace creates the new palace at
the **HOME level** (`<home>/chroma.sqlite3`), not at `<home>/palace/`. Importing
into an already-initialized palace appends at `palace_path` correctly, but
`--create-new-palace` into a raw dir hits this quirk — so the import warns when
the expected Chroma DB is still missing afterward. Prefer initializing a new
palace through normal mempalace channels, then importing into it.

## See also

- [`../scripts/palace_wing.py`](../scripts/palace_wing.py) — `resolve_palace_layout`,
  `bind_palace`, `preflight_import_target`.
- [`SKILL.md`](../SKILL.md) — export/import usage.
- [`../../mempalace-restore/SKILL.md`](../../mempalace-restore/SKILL.md) — import side.
