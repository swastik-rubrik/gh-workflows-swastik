# JFrog Images Publish

Copies first-party container images from GCP Artifact Registry into JFrog Artifactory, comparing digests so a run only transfers what is missing or wrong.

Teams declare their images in a YAML file in their own repo and call the reusable workflow at `.github/workflows/jfrog-images-onboarding.yml`.

## What this does

- Skips images already present with a matching digest, so a no-op run transfers nothing.
- Checks all of a team's images every run, so anything deleted or half-copied at the destination is repaired.
- Uses `crane copy`, preserving the source digest and the multi-arch index.
- No static credentials — WIF for GCP, Vault over GitHub OIDC for Artifactory.
- Rejects bad specs at plan time: no `latest`, no tag inside `source_image`.
- Writes a per-image table to the job summary.

**One team per run.** The Vault role is scoped to one team, so a run holds exactly one team's Artifactory token and can push only to that team's local repo. Several teams means several runs.

## Prerequisites

- `id-token: write` on the calling job — WIF and Vault both need the OIDC token.
- A GCP Workload Identity Provider scoped to `attribute.repository`, and a service account with `artifactregistry.reader`.
- An Artifactory local repo, push token, and the Vault entry holding it (provisioned by IT/IPE). See `docs/vault-setup.md`.

## Usage

### 1. Add `teams/<your-team>.yaml`

```yaml
team:        infosec-sre
target_repo: infosec-sre-local

gcp_wif_provider: projects/123456789012/locations/global/workloadIdentityPools/gh-pool/providers/gh-provider
gcp_sa_email:     gar-reader@my-gcp-project.iam.gserviceaccount.com

images:
  - source_image: us-docker.pkg.dev/my-gcp-project/my-registry/my-app
    target_path:  my-app/server
    tag:          cc1546d2825a0b44e078d594d77a5a95bd4b4ba5
    environments: [dev, prod]
```

`vault_path` and `vault_role` are derived (`rubrik-secret/data/infosec/<environment>/<team>/jfrog-artifactory` and `<team>`); declare them only to sit outside that convention. The file's `team:` must equal the filename.

### 2. Call the workflow

```yaml
on:
  workflow_dispatch:
    inputs:
      environment: { type: string, default: dev }
      dry_run:     { type: boolean, default: false }
  pull_request:
    paths: ['teams/**']

jobs:
  onboard:
    uses: swastik-rubrik/gh-workflows-swastik/.github/workflows/jfrog-images-onboarding.yml@main
    permissions:
      contents: read
      id-token: write
    with:
      team: infosec-sre
      teams_dir: teams
      artifactory_registry: myorg.jfrog.io
      vault_url: https://vault.example.com
      environment: ${{ inputs.environment || 'dev' }}
      # PRs are always plan-only
      dry_run: ${{ github.event_name == 'pull_request' || inputs.dry_run }}
```

No `secrets:` block — there is nothing static to pass. Declare the `workflow_dispatch` inputs or `inputs.dry_run` is empty; keep `paths:` on the PR trigger or every unrelated PR runs a Vault login.

## Team file fields

| Field | Required | Notes |
| --- | --- | --- |
| `team` | yes | Credential boundary. Must equal the filename |
| `target_repo` | yes | Artifactory repo key |
| `gcp_wif_provider` | yes | Workload Identity Provider resource name |
| `gcp_sa_email` | yes | Service account to impersonate |
| `gcp_project` | no | Inferred from the first `source_image` |
| `vault_path` / `vault_role` | no | Derived from `<environment>`/`team` |

Per image: `source_image` (host + path, **no tag**), `target_path`, `tag` (immutable, `latest` rejected) are required; `environments` (absent means all) and `service` are optional.

The source host is read from `source_image`, not configurable. A team spanning multiple GCP regions is rejected — split into separate files.

## Workflow inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `team` | yes | — | Filename in `teams_dir` without extension. One per run |
| `artifactory_registry` | yes | — | Destination registry host |
| `vault_url` | yes | — | Vault address |
| `teams_dir` | no | `teams` | Directory **in the calling repo** |
| `environment` | no | `prod` | Filters images, and selects the Vault path |
| `dry_run` | no | `false` | Resolve digests and report, copy nothing |
| `artifactory_path_style` | no | `false` | Prepend the repo key instead of subdomain routing |
| `vault_jwt_mount` | no | `jwt-github` | Vault JWT auth mount |
| `action_ref` | no | `main` | Ref of this repo — pin to a tag in production |

`environment` is not just a label: `dev` and `prod` are separate Vault entries with separate JFrog tokens.

The composite action at `actions/jfrog-images-publish` takes the same values flattened, plus `vault_path` and `vault_role` per team. Call the workflow unless you need the step directly.

## Job summary

Every team's runs go through this shared workflow, so the run report is where activity is recorded in a common shape. One row per image:

```text
### infosec-sre — prod

2 copied · 1 already correct · 1 failed

| image | outcome | digest |
| --- | --- | --- |
| `my-app/server:cc1546d2` | copied (new)    | `9f2a1c0b4de8` |
| `my-app/agent:a71b39f4` | already correct | `3c0d81ea77b2` |
```

Outcomes: `copied (new)`, `copied (digest mismatch)`, `already correct`, `would copy (…)` under `dry_run`, and `unreadable at source` / `copy failed` / `verify failed`.

## Notes

- `team` is matched against the directory listing, not joined into a path, so it cannot escape `teams_dir`. The `team:` field must equal the filename because the Vault role derives from the field while the run was authorised against the name.
- Public images (`docker.io`, `quay.io`, …) must not be listed — Artifactory remote repos proxy them on demand. Enforced in review.
- Pull through the virtual repo: `docker-virtual.myorg.jfrog.io/<target_path>:<tag>`. Note: when a path exists in both a local and a remote repo behind the virtual, resolution order decides which you get.
- Scope the WIF provider to `attribute.repository`; a pool trusting the whole org lets any repo read your registry.
- Pin `action_ref` to a version tag in production.

## Troubleshooting

**`no OIDC token available`** — the job is missing `permissions: id-token: write`.

**Vault `400` / `permission denied`** — check `vault_jwt_mount`, that a role named after the team exists, and that its policy grants `read` on `rubrik-secret/data/infosec/<env>/<team>/jfrog-artifactory`.

**`no team file for 'x'`** — `team` matches no filename in `teams_dir`. It is the filename, not the `team:` field.

**`declares team 'x' but the file is named 'y'`** — the field and filename disagree; the run would authenticate as a different team than requested.

**`images span multiple source registries`** — split into separate team files.

**Empty matrix** — no images list the chosen `environment`. Expected; the publish job is skipped.

**`tag moved at source`** — the source tag was re-pushed. The copy proceeds, but tags should be immutable; investigate the source.
