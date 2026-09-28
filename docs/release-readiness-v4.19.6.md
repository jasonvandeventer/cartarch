# v4.19.6 release readiness

Prepared locally on 2026-09-28. No production deployment is authorized by this preparation record.

## Candidate and scope

Based on origin/main `0db4768688186a3ef2bae80b81ff2b9f081cd43c`; fetched before preparation, with no intervening changes. Usability implementation: `1ecf983`. This release adds no migration and changes no dependency requirements. README and Chronicle carry v4.19.6.

Decision: unverified seed imports remain skipped. Existing profiles/results and pilot edits are preserved. Automatic seed refresh can resume for entries with a verified `_identity` owner/deck mapping. No production identity data was read during preparation.

## Read-only production checks

- Deployment: v4.19.5, one ready replica.
- PostgreSQL cartarch-prod: healthy, three ready instances.
- Backup `cartarch-prod-daily-20260928030000`: completed 2026-09-28T03:00:45Z.
- LastBackupSucceeded and ContinuousArchiving conditions: True.
- Rollback image: `ghcr.io/jasonvandeventer/cartarch@sha256:b16a380f20876c9749dac01457b109d28bd922cc206d532d6695dff2705c6129` (v4.19.5).

These are status checks, not a fresh restore drill. Recheck freshness if deployment is delayed.

## Rollback

Use the platform release-retraction runbook. Its automatic tag-removal convergence path is explicitly marked unverified; the documented fallback first disables both ArgoCD auto-sync and image-updater promotion before pinning the previous immutable image. Do not rely on a bare `kubectl rollout undo` while both reconcilers are active. No schema rollback is needed for this release.

Runbook: `/home/jason/lab/vanfreckle-platform/docs/runbooks/release-retraction.md`.

## Publication after approval

Fast-forward main only if still compatible, create/push the single v4.19.6 release tag, and allow the existing workflow gates to pass. The publisher must load the saved, tested image artifact rather than rebuild it. Observe rollout, public /version and /health, and the deployment stability checks. Verify collection no-match recovery, import controls, and deck controls after rollout.

Local final-commit gate results and the candidate image identity are recorded in the preparation report accompanying this commit. GitHub publication gates will still run when the approved release is pushed.
