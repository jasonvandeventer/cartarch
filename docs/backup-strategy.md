# Database backup and recovery

Cartarch production uses **PostgreSQL 18 on CloudNativePG**, cluster
`cartarch-prod` in namespace `cnpg-system`. SQLite is for local development.
The former SQLite/Longhorn recovery procedure is not the production database
recovery procedure.

Configuration verified against the platform repository on 2026-09-28:

- Three database instances, with separate Longhorn-backed volumes.
- Barman Cloud plugin continuously archives WAL to Cloudflare R2.
- Daily base backup at 03:00 UTC; retention is 90 days.
- Backup prefix: `s3://cartarch-cnpg-backups/cartarch-prod/`.
- ObjectStore: `cnpg-r2-store` in `cnpg-system`.

The authoritative [CNPG restore runbook](https://github.com/jasonvandeventer/vanfreckle-platform/blob/main/docs/cnpg-dr-restore.md)
contains verification commands, a disposable restore drill, and disaster/PITR
procedures. Recovery-point exposure depends on the newest archived WAL, not just
the base-backup schedule. Replicas and volume snapshots do not replace this backup chain.

The runbook records a **2026-07-20** drill: 99-second data-layer recovery, matching
schema revision, row counts, and latest transaction timestamp. This is historical
validation, not a claim that a new drill or backup-health check ran on 2026-09-28.

Before relying on a backup, follow the runbook to verify a recent base backup and
advancing WAL. Rehearse into a disposable cluster and compare schema revision,
application row counts, and latest transaction time before routing application traffic.
The disposable restore must not archive WAL to the production backup prefix.

Configuration sources:

- [Production cluster](https://github.com/jasonvandeventer/vanfreckle-platform/blob/main/clusters/talos/manifests/cnpg-cartarch-prod/cluster.yaml)
- [Scheduled backup](https://github.com/jasonvandeventer/vanfreckle-platform/blob/main/clusters/talos/manifests/cnpg-cartarch-prod/scheduledbackup.yaml)
- [Backup ObjectStore](https://github.com/jasonvandeventer/vanfreckle-platform/blob/main/clusters/talos/manifests/cnpg-backup-config/cnpg-objectstore.yaml)
