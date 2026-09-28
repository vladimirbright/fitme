#!/bin/sh
# Cron-friendly backup wrapper. Run from the directory containing compose.yaml (or set
# COMPOSE_FILE/--project-directory), e.g. via crontab:
#
#   0 3 * * * cd /opt/fitme && ./deploy/backup.sh >> /var/log/fitme-backup.log 2>&1
#
# `fitme backup` uses SQLite's own online backup API (A§4.6, M10): it's safe to run this
# while the stack keeps serving.
set -eu

docker compose exec -T fitme fitme backup --out /data/backups --keep 14

# The backup lands inside the fitme-data volume, next to the live database, which is fine for
# "restore inside this same host" but not for off-host safety. To also copy the newest backup
# out onto the host filesystem (e.g. before shipping it elsewhere):
#
#   docker compose cp fitme:/data/backups/. ./backups/
