# Database backups

`mongodump --archive --gzip` snapshots of the `bank_analyzer` database, taken
before any migration that rewrites transaction categories.

The archives themselves are **not** in version control (see `.gitignore`) —
they contain the full transaction history.

## Taking a backup

```bash
docker exec mongo-bank mongodump --db=bank_analyzer --archive --gzip \
  > "backups/bank_analyzer-$(date +%Y%m%d-%H%M%S).archive.gz"
```

## Restoring one

`--drop` replaces the existing collections, so the database ends up exactly as
it was when the archive was written:

```bash
docker exec -i mongo-bank mongorestore --archive --gzip --drop \
  < backups/bank_analyzer-YYYYMMDD-HHMMSS.archive.gz
```
