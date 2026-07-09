# Palace layout & the Chroma-vs-KG path footgun

A local MemPalace lives under a **HOME** dir (default `~/.mempalace`). Two stores
sit under HOME and resolve their paths by **different** rules — the single most
common way to silently corrupt a wing export/import.

```
~/.mempalace/                         <- HOME  (config/home dir; pass as --palace)
├── config.json                       <- palace_path -> ~/.mempalace/palace
├── knowledge_graph.sqlite3           <- KG (HOME-level!)
├── tunnels.json
├── wal/  locks/
└── palace/                           <- the ChromaDB "palace" dir
    ├── chroma.sqlite3                <- vectors + drawer metadata
    ├── <uuid>/*.bin                  <- HNSW index
    └── .mempalace/origin.json        <- embedder identity (critical)
```

## The two resolution rules (they disagree)

The running MCP server (`mempalace-mcp`, launched **without** `--palace`) reads:

- **Chroma** at `<palace_path>/chroma.sqlite3`, where `palace_path` is
  `config.json`'s `palace_path` (default `<home>/palace`). `palace_path` is
  overridable via `MEMPALACE_PALACE_PATH` (env **wins** over config).
- **KnowledgeGraph** at the **HOME-level** `<home>/knowledge_graph.sqlite3`
  (`mcp_server._resolve_kg_path` returns the HOME default unless the server
  itself was given `--palace`).

So Chroma is `palace_path`-relative but the KG is HOME-relative. A tool that sets
`MEMPALACE_PALACE_PATH` to HOME (thinking "palace = HOME") sends Chroma writes to
a **stray** `<home>/chroma.sqlite3` the server never reads, while the KG still
lands correctly — a split-brain that looks half-right.

## Consequences for `palace_wing.py`

- `--palace` is the **HOME** dir (`~/.mempalace`), never the nested
  `~/.mempalace/palace` DB dir.
- `resolve_palace_layout(home)` centralizes both rules → `(chroma_dir, kg_path)`.
  `bind_palace` points `MEMPALACE_PALACE_PATH` at the resolved **chroma dir**
  (not HOME); `open_kg` uses the HOME-level KG. This is why a single correct
  `--palace ~/.mempalace` now imports **both** drawers and KG where the server
  reads them.
- The import **preflight** aborts when the resolved Chroma DB does not exist
  (the "`--palace` points at the DB dir" mistake), unless `--create-new-palace`.

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
