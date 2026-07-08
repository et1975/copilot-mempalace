# Restic Cheatsheet for MemPalace

Restic deduplicates and encrypts backups by default. This skill uses **local
repos only**: an external drive or another folder.

## Environment

```bash
export RESTIC_REPOSITORY=/mnt/backup/mempalace-restic
export RESTIC_PASSWORD_FILE=~/.config/mempalace-restic.pass
chmod 600 "$RESTIC_PASSWORD_FILE"
```

Set the repo path and password file. Never hard-code the password; avoid
`--insecure-no-password`.

## Repository lifecycle

```bash
restic init
```

Initialize the local repo once.

```bash
restic unlock
```

Remove stale **restic repo** locks only after confirming no restic process is
running. This is unrelated to `~/.mempalace/locks`, which backups exclude.

## Backup

```bash
restic backup ~/.mempalace \
  --exclude ~/.mempalace/locks \
  --tag palace \
  --tag "$(hostname)"
```

Create an on-demand palace snapshot; always exclude locks and keep
`palace/.mempalace/origin.json` included.

## Browse and search snapshots

```bash
restic snapshots --tag palace
```

List palace snapshots.

```bash
restic ls latest
```

List files in the latest snapshot.

```bash
restic find palace/.mempalace/origin.json
```

Find critical files across snapshots.

```bash
restic diff <snapshot-a> <snapshot-b>
```

Compare two snapshots.

## Retention and pruning

```bash
restic forget --keep-last N --keep-daily D --keep-weekly W --prune
```

Apply manual retention and prune unreferenced data in one command.

```bash
restic prune
```

Prune unreferenced data after separate `forget` runs.

## Integrity checks

```bash
restic check
```

Verify repository metadata and structure.

```bash
restic check --read-data-subset=5%
```

Read-check a sampled subset of packed data for deeper periodic verification.

## Second local copy

```bash
export RESTIC_REPOSITORY=/mnt/backup/mempalace-restic-copy
export RESTIC_PASSWORD_FILE=~/.config/mempalace-restic-copy.pass
restic init
```

Initialize the destination local repo.

```bash
restic copy \
  --from-repo /mnt/backup/mempalace-restic \
  --from-password-file ~/.config/mempalace-restic.pass
```

Copy snapshots from one local repo to another local repo.

## Stats

```bash
restic stats latest
```

Show size statistics for the latest snapshot.

```bash
restic stats --mode raw-data
```

Show repository storage use after deduplication.
