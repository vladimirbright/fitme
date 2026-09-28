# Restoring a backup

`fitme backup` (see `deploy/backup.sh`, README.md) writes consistent, restorable copies of
the whole database into `/data/backups`, inside the `fitme-data` volume, mode 0600.

Two cases: restoring one of those backups in place (the common case — nothing ever left the
volume), or restoring from a copy you took off this host (e.g. `docker compose cp`'d
elsewhere, or from a fresh host).

## Restoring a backup already in the volume

1. **Stop the stack.** This keeps the `fitme-data` volume but removes the running container,
   so nothing else has the database file open while it's replaced:

   ```sh
   docker compose down
   ```

2. **Replace the live database with the backup**, running as the app's own user (uid 10001 —
   `docker compose run` uses the image's default user, the same one `fitme serve` itself runs
   as) so the restored file ends up owned correctly, not root:root. Remove any leftover
   `-wal`/`-shm` files first: SQLite would otherwise replay old, now-mismatched WAL frames
   onto the freshly-restored file the moment it's opened.

   ```sh
   docker compose run --rm --no-deps -T --entrypoint sh fitme -c '
     rm -f /data/fitme.db /data/fitme.db-wal /data/fitme.db-shm &&
     cp /data/backups/fitme-YYYYmmdd-HHMMSS.db /data/fitme.db
   '
   ```

   Replace the filename with the backup you actually want (list them first: `docker compose
   run --rm --no-deps -T --entrypoint sh fitme -c 'ls -la /data/backups'`).

3. **Start the stack again:**

   ```sh
   docker compose up -d
   ```

   The entrypoint runs `fitme db upgrade` before `fitme serve` starts, so a backup taken on
   an older schema version is migrated forward automatically — restoring an older backup is
   safe even after the app has since gained new migrations.

## Restoring from a copy on the host

If the backup file lives on the host instead (copied out earlier, or you're restoring onto a
fresh host from an off-site copy): put it in `deploy/restore-staging/` (gitignored —
`fitme-*.db` is health data), then, after `docker compose down`:

```sh
docker run --rm \
  --user 10001:10001 \
  -v fitme_fitme-data:/data \
  -v "$(pwd)/deploy/restore-staging":/backup:ro \
  alpine sh -c '
    rm -f /data/fitme.db /data/fitme.db-wal /data/fitme.db-shm &&
    cp /backup/fitme-YYYYmmdd-HHMMSS.db /data/fitme.db
  '
```

`--user 10001:10001` matters here: a plain `docker run` on the generic `alpine` image
defaults to root, and a `cp` run as root would leave `/data/fitme.db` owned by root:root —
unreadable/unwritable by the app's own uid 10001, which would then crash-loop. Replace
`fitme_fitme-data` with your project's actual volume name if different — Compose prefixes
volume names with the project/directory name; list them with `docker volume ls`. Then:

```sh
docker compose up -d
```
