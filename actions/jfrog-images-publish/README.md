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

**One team per job.** The Vault role is scoped to a team, so each matrix job holds exactly one team's Artifactory token and can push only to that team's local repo. Omitting `team` reconciles every file in `teams_dir`, but each still runs as its own job with its own credentials — the boundary is the job, not the run.

## Prerequisites

- `id-token: write` on the calling job — WIF and Vault both need the OIDC token.
- A GCP Workload Identity Provider scoped to `attribute.repository`, and a service account with `artifactregistry.reader`.
- An Artifactory local repo, push token, and the Vault entry holding it (provisioned by IT/IPE). See `docs/vault-setup.md`.

## Usage

### 1. Add `teams/<your-team>.yaml`

A team declares its identity once, then one or more `projects:`, each with its own GCP credentials and images — so one team can pull from several GCP projects.

```yaml
team:        infosec-sre          # selects the Vault role and secret path
target_repo: infosec-sre-dev-local

projects:
  - gcp_project:      my-gcp-project
    gar_registry:     asia-south1-docker.pkg.dev   # the region your GAR repo is in
    gar_repo:         my-registry
    gcp_wif_provider: projects/123456789012/locations/global/workloadIdentityPools/gh-pool/providers/gh-provider
    gcp_sa_email:     gar-reader@my-gcp-project.iam.gserviceaccount.com

    images:
      - my-app:cc1546d2825a0b44e078d594d77a5a95bd4b4ba5
      - my-agent:v1
```

Each image is `name:tag`, resolved against the project's `gar_registry` + `gcp_project` + `gar_repo` into a full `source_image`, with `target_path` defaulting to the name.

`gar_registry` is stated per project and has **no default**. A default would live in this shared repo, invisible to the team whose images it resolves, and getting it wrong is silent — the run would build a plausible path in the wrong region and only fail later at pull time. It is `<location>-docker.pkg.dev`, where `<location>` is the region the repo was created in (`gcloud artifacts repositories list`). A team pulling from several regions gives each its own `projects:` entry.

An image whose Artifactory path differs, or that lives outside the project's `gar_repo`, spells itself out:

```yaml
    images:
      - name:        my-app                    # split form
        tag:         v1
        target_path: infra/my-app              # differs from the name

      - source_image: asia-south1-docker.pkg.dev/other-proj/other-repo/thing
        tag:          v2                       # explicit: another GCP project
        environments: [dev, prod]
```

The three forms can be mixed freely in one list. Validation is identical for all of them — `latest` and a tag inside `source_image` are rejected whichever form you use.

An explicit `source_image` may point at a **different GCP project or GAR repo**, as above, but every image in one `projects:` entry must share the same registry **host**: one entry authenticates as one service account against one registry. Mixing `asia-south1-` and `us-docker.pkg.dev` in a single entry is rejected at plan time —

```text
project 'my-gcp-project' spans multiple source registries
(asia-south1-docker.pkg.dev, us-docker.pkg.dev); split them into
separate `projects:` entries
```

— so a different region needs its own entry with its own `gar_registry` and credentials.

`vault_path` and `vault_role` are derived (`rubrik-secret/data/infosec/<environment>/<team>/jfrog-artifactory` and `<team>`); declare them only to sit outside that convention. The file's `team:` must equal the filename.

**The image list is the review control.** It is deliberately explicit rather than discovered from GAR: with auto-discovery, anyone with push access to the registry could land an image in Artifactory without review, and `dry_run` would stop being a meaningful preview.

### 2. Call the workflow

```yaml
on:
  pull_request:                  # validate + report, never pushes
    paths: ['teams/**']
  push:                          # merge reconciles for real
    branches: [main]
    paths: ['teams/**']
  workflow_dispatch:
    inputs:
      environment: { type: string,  default: dev }
      dry_run:     { type: boolean, default: true }

jobs:
  onboard:
    uses: swastik-rubrik/gh-workflows-swastik/.github/workflows/jfrog-images-onboarding.yml@main
    permissions:
      contents: read
      id-token: write
    with:
      vault_url: https://vault.example.com   # the only required input
      environment: ${{ inputs.environment || 'dev' }}
      # A PR is always plan-only; a push to main copies; a dispatch honours
      # the checkbox. On non-dispatch events every `inputs.*` is null.
      dry_run: ${{ github.event_name == 'pull_request' || inputs.dry_run }}
```

No `secrets:` block — there is nothing static to pass. Everything except `vault_url` has a default, so a caller is usually this short. Omitting `team` reconciles every file in `teams_dir`.

Keep `paths:` on the PR trigger, or every unrelated PR runs a Vault login.

> **Careful with the `dry_run` expression.** Do not "clarify" it as
> `pull_request || (workflow_dispatch && inputs.dry_run)`. In GitHub
> expressions `a && b` evaluates to **`a`** when `b` is falsy, so an unticked
> checkbox yields the truthy string `'workflow_dispatch'` and silently forces a
> dry run — the reconcile then never copies anything. The form above relies on
> `inputs.dry_run` being null (falsy) on non-dispatch events, which is correct.

If a caller offers a `team` dropdown, remember GitHub resolves `options:` when it
parses the workflow — it cannot be generated at run time. Check it against
`teams/` in CI instead, or a new team file becomes unreachable from the UI.

## Team file fields

At the root of the file:

| Field | Required | Notes |
| --- | --- | --- |
| `team` | yes | Credential boundary. Must equal the filename |
| `target_repo` | yes | Artifactory repo key |
| `projects` | yes | One or more source projects |
| `vault_path` / `vault_role` | no | Derived from `<environment>`/`team` |

Per entry in `projects:`:

| Field | Required | Notes |
| --- | --- | --- |
| `gcp_wif_provider` | yes | Workload Identity Provider resource name |
| `gcp_sa_email` | yes | Service account to impersonate |
| `images` | yes | See the three forms above |
| `gcp_project` | for shorthand | Also inferred from an explicit `source_image` |
| `gar_repo` | for shorthand | GAR repository holding the images |
| `gar_registry` | for shorthand | Registry host, `<location>-docker.pkg.dev`. No default — the region is never assumed |

Per image, after expansion: `source_image` (host + path, **no tag**), `target_path`, `tag` (immutable, `latest` rejected) are required; `environments` (absent means all) and `service` are optional. Shorthand supplies the first two.

A project spanning multiple GCP regions is rejected — give it its own entry with its own `gar_registry`.

## Workflow inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `vault_url` | yes | — | Vault address. The only required input |
| `team` | no | `''` | Filename in `teams_dir` without extension. Empty reconciles every file |
| `artifactory_registry` | no | `beelzi.jfrog.io` | Destination registry host |
| `teams_dir` | no | `teams` | Directory **in the calling repo** |
| `environment` | no | `prod` | Filters images, and selects the Vault path |
| `dry_run` | no | `false` | Resolve digests and report, copy nothing |
| `artifactory_path_style` | no | `true` | Prepend the repo key instead of subdomain routing |
| `vault_jwt_mount` | no | `github-jwt` | Vault JWT auth mount |
| `action_ref` | no | `main` | Ref of this repo — pin to a tag in production |

`vault_url` deliberately has no default: the current dev instance is a Cloudflare tunnel whose hostname changes on every restart, so a baked-in value would send runs at a dead host and fail confusingly at the login step.

`artifactory_path_style` defaults to `true` because the free JFrog instance has no per-repo subdomains.

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

**`no OIDC token available`** — the calling repository is missing
`permissions: id-token: write` at the **workflow level**. A job that is only
`uses: <reusable workflow>` runs with the permissions its caller granted at
workflow level; a `permissions:` block on that job can restrict them but cannot
add `id-token`. Putting the grant only on the job is the usual cause — the run
then logs just `Contents: read` / `Metadata: read` and `OIDC_URL` stays empty.

**Vault `400` / `permission denied`** — check `vault_jwt_mount`, that a role named after the team exists, and that its policy grants `read` on `rubrik-secret/data/infosec/<env>/<team>/jfrog-artifactory`.

**`no team file for 'x'`** — `team` matches no filename in `teams_dir`. It is the filename, not the `team:` field.

**`declares team 'x' but the file is named 'y'`** — the field and filename disagree; the run would authenticate as a different team than requested.

**`spans multiple source registries`** — one `projects:` entry mixes two registry hosts. Split it into one entry per host; each authenticates separately.

**``needs `gar_registry` on the project``** — a shorthand image with no registry to resolve against. Add the host your GAR repo lives in, or give that image an explicit `source_image`.

**Empty matrix** — no images list the chosen `environment`. Expected; the publish job is skipped.

**`tag moved at source`** — the source tag was re-pushed. The copy proceeds, but tags should be immutable; investigate the source.
