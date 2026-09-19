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
