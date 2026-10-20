# Tutor production image and deployment

`Build Tutor Learning` builds the LMS branch `tutor-main` and publishes:

- `ghcr.io/sergey-olshansky/tutor-learning:lms-<LMS SHA>-infra-<infra SHA>`;
- `ghcr.io/sergey-olshansky/tutor-learning:main`.

The workflow can also be started manually with an explicit `image_tag`.

## Deploy a published tag

Run from a trusted operator machine with working `gh` and `ssh tutor-vps`
authentication:

```bash
./deploy-production.sh lms-a1d219ad-infra-01234567
```

The script validates and pulls the exact tag before changing production. It
uses a temporary GHCR login on the VPS, creates a Frappe backup including site
files, saves the previous `.env`, atomically changes `CUSTOM_TAG`, recreates
only application containers (not MariaDB or Redis), runs migrations, clears
cache, and checks the public HTTPS endpoint.

If container recreation fails before migrations, the previous tag is restored
automatically. Once migrations begin, rollback is intentionally not automatic:
the script prints the retained backup path so schema recovery can be planned
without overwriting user data.

## Production image retention

Both `deploy-production.sh` and the production Actions workflow use
`scripts/retain-production-images.sh`. Before pulling, they remove older local
images from the Tutor Learning repository, preserving the running image and
the incoming image if already present. After migration and the public HTTP
health check succeed, they retain the released image and its predecessor.

The predecessor is recorded in `.previous-production-tag`; retrying the same
release preserves that predecessor. Existing hosts bootstrap this record from
their `.env.pre-deploy-*` backups. Failed deployment attempts retain the running
and incoming images and leave their retention plan for recovery.

Retention compares image IDs, so aliases of either retained image are safe.
Images referenced by running or stopped containers are skipped with a warning.
Docker removal also runs without force. Other image repositories, containers, volumes, site files and backups
are not pruned. This policy affects local Docker storage, not GHCR history.

## Build once and promote

`build.yml` reuses a GHCR image keyed by the full LMS SHA and build recipe.
The recipe includes the Containerfile, apps configuration, verification workflow,
registry tooling, frappe_docker revision and Frappe selector. An existing image
is copied to the run-specific immutable tag and verified, without compiling.
The cache-bust value and local pinned-source Git commit are deterministic.
Duplicate builds for one revision are serialized; the second reuses the image.
Optional GHA layer-cache export is limited to two minutes and does not block
a verified image when cache export fails.
Change the dependency selector/recipe when deliberately refreshing dependencies.

After developer preprod deployment, the human and independent tester inspect
one SHA/tag/digest in parallel. Both approvals are required before release prep.
For an accepted candidate whose complete Git tree equals the merge commit tree,
dispatch `build.yml` on main with `promote_from` (accepted digest), `source_tag`,
`source_sha` and `lms_sha` (merged SHA). Promotion verifies original successful
build provenance and its VERIFIED artifact, checks full Git tree equality and
publishes the production tag by copying the raw manifest bytes. Its digest must
remain unchanged. A changed merge tree needs a new candidate and both approvals.

Promotion does not deploy production. The normal human release command,
backup, migration, health and image-retention steps still apply.
