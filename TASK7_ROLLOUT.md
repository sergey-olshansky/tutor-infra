# Task #7 release-contract rollout

PR #1 must be merged before `/pipeline` is enabled. A workflow dispatched with
`ref=main` before that merge uses the old `main` workflow, irrespective of this
PR's branch build. Keep `/opt/release-ops/release_ops.py` unchanged until then.

1. Review the branch build and its exact LMS SHA. Merge PR #1 only through the
   release process (this PR does not authorize a deploy).
2. Record the resulting **infra main commit SHA**. Replace
   `REPLACE_WITH_MERGED_INFRA_SHA` in `release_ops.task7.patch`'s resulting file
   with that exact SHA, apply it to `/opt/release-ops/release_ops.py`, syntax-check,
   then restart release-ops. The SHA gate fails closed until this is done.
3. `/pipeline` sends LMS branch plus its resolved full 40-character SHA and a
   random dispatch ID. It accepts only the matching first-attempt run on infra
   `main` at the pinned infra SHA. The run-name carries the dispatch ID and
   inputs; `head_sha`, `head_branch`, event and attempt are checked separately.
   It looks up the exact tag, then dispatches preprod with a distinct request ID.

Task image: `task-<N>-lms-<sha12>-infra-<sha12>-run-<runid>-<attempt>`.
Production candidate: `lms-<sha8>-infra-<sha8>-run-<runid>-<attempt>`.
Both are minted once per workflow attempt, so a repeated dispatch has a new tag.
The task image comes from `task/N`; production candidate comes only from
`infra/main` with LMS `tutor-main`. Build assembles a local git repository from
GitHub's exact LMS commit archive; bench clones it, not a moving LMS branch.
`main` alias is only refreshed by an infra `main` push.

No production candidate is valid before the changed production workflow and
release-bot parser are installed together. The branch build does not activate
`/pipeline` or authorize preprod/production deployment.
