# JFrog Images Publish

Copies first-party container images from GCP Artifact Registry into JFrog Artifactory, comparing digests so a run only transfers what is missing or wrong.

A repo lists its images in one `images.yaml` and calls the reusable workflow at `.github/workflows/jfrog-images-onboarding.yml`. Every image lands in the single Artifactory repo that file names.

## What this does

- Skips images already present with a matching digest, so a no-op run transfers nothing.
- Checks every listed image on every run, so anything deleted or half-copied at the destination is repaired.
- Uses `crane copy`, preserving the source digest and the multi-arch index.
- Reads GCP through Workload Identity Federation (no key files). Pushes to Artifactory with one username and token, held as repository secrets.
- Rejects bad specs at plan time: no `latest`, no tag inside `source_image`, no two images publishing the same `target_path:tag`.
- Writes a per-image table to the job summary.

Each GCP project in the file runs as its own parallel job, because each authenticates as a different service account.

## Prerequisites

- `permissions: id-token: write` at the **workflow level** of the caller. WIF needs the OIDC token.
- Per GCP project: a Workload Identity Provider scoped to `attribute.repository`, and a service account with `artifactregistry.reader`.
- An Artifactory local repo (e.g. `infosec-local`), and a user plus access token with deploy rights on it.
- Two repository secrets in the calling repo: `JFROG_USERNAME` and `JFROG_TOKEN`.

## Usage

### 1. Add `images.yaml`

The file names the destination repo once, then lists one or more `projects:`. Each project has its own GCP credentials and images, so one file can pull from several GCP projects.

```yaml
target_repo: infosec-local

projects:
  - gcp_project:      my-gcp-project
    gar_registry:     asia-south1-docker.pkg.dev   # the region your GAR repo is in
    gar_repo:         my-registry
    gcp_wif_provider: projects/123456789012/locations/global/workloadIdentityPools/gh-pool/providers/gh-provider
    gcp_sa_email:     gar-reader@my-gcp-project.iam.gserviceaccount.com

    images:
      - my-app:cc1546d2825a0b44e078d594d77a5a95bd4b4ba5
      - my-agent:v1

  - gcp_project:      another-project
    gar_registry:     asia-south1-docker.pkg.dev
    gar_repo:         other-registry
    gcp_wif_provider: projects/210987654321/locations/global/workloadIdentityPools/gh-pool/providers/gh-provider
    gcp_sa_email:     gar-reader@another-project.iam.gserviceaccount.com

    images:
      - other-app:v3
```

Each image is `name:tag`. It resolves against the project's `gar_registry`, `gcp_project` and `gar_repo` into a full `source_image`, and `target_path` defaults to the name.

`gar_registry` is stated per project and has **no default**. A default would live in this shared repo, where the repo whose images it resolves can't see it. Getting it wrong is also silent: the run builds a plausible path in the wrong region and only fails later, at pull time. It is `<location>-docker.pkg.dev`, where `<location>` is the region the repo was created in (`gcloud artifacts repositories list`).

Some images need to spell themselves out: one whose Artifactory path differs from its name, or one that lives outside the project's `gar_repo`.

```yaml
    images:
      - name:        my-app                    # split form
        tag:         v1
        target_path: infra/my-app              # differs from the name

      - source_image: asia-south1-docker.pkg.dev/other-proj/other-repo/thing
        tag:          v2                       # explicit: another GCP project
```

The three forms can be mixed freely in one list. Validation is identical for all of them: `latest` and a tag inside `source_image` are rejected whichever form you use.

An explicit `source_image` may point at a **different GCP project or GAR repo**, as above. Every image in one `projects:` entry must still share the same registry **host**, because one entry authenticates as one service account against one registry. Mixing `asia-south1-` and `us-docker.pkg.dev` in a single entry is rejected at plan time:

```text
project 'my-gcp-project' spans multiple source registries
(asia-south1-docker.pkg.dev, us-docker.pkg.dev); split them into
separate `projects:` entries
```

A different region therefore needs its own entry, with its own `gar_registry` and credentials.

Everything lands in one repo, so every `target_path:tag` must be unique across the whole file, including across projects. Otherwise two sources would overwrite one destination on every run. A duplicate fails at plan time and names both entries.

**The image list is the review control.** It is deliberately explicit rather than discovered from GAR. With auto-discovery, anyone with push access to the registry could land an image in Artifactory without review, and `dry_run` would stop being a meaningful preview.

### 2. Add the repository secrets

In the calling repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Value |
| --- | --- |
| `JFROG_USERNAME` | The Artifactory user the token belongs to |
| `JFROG_TOKEN` | An access token with deploy rights on `target_repo` |

Or from the CLI, where each command prompts for the value so it stays out of shell history:

```sh
gh secret set JFROG_USERNAME --repo <owner>/<repo>
gh secret set JFROG_TOKEN    --repo <owner>/<repo>
```

### 3. Call the workflow

```yaml
on:
  pull_request:                  # validate + report, never pushes
    paths: [images.yaml]
  push:                          # merge reconciles for real
    branches: [main]
    paths: [images.yaml]
  workflow_dispatch:
    inputs:
      dry_run: { type: boolean, default: true }

permissions:
  contents: read
  id-token: write                # GCP Workload Identity Federation

jobs:
  onboard:
    uses: swastik-rubrik/gh-workflows-swastik/.github/workflows/jfrog-images-onboarding.yml@main
    permissions:
      contents: read
      id-token: write
    with:
      # A PR is always plan-only; a push to main copies; a dispatch honours
      # the checkbox. On non-dispatch events every `inputs.*` is null.
      dry_run: ${{ github.event_name == 'pull_request' || inputs.dry_run }}
    secrets:
      JFROG_USERNAME: ${{ secrets.JFROG_USERNAME }}
      JFROG_TOKEN: ${{ secrets.JFROG_TOKEN }}
```

The secret names are uppercase and match the repo secrets, so `secrets: inherit` works too if you prefer it.

Keep `paths:` on the PR trigger, or every unrelated PR runs the workflow.

> **Careful with the `dry_run` expression.** Do not "clarify" it as
> `pull_request || (workflow_dispatch && inputs.dry_run)`. In GitHub
> expressions `a && b` evaluates to **`a`** when `b` is falsy, so an unticked
> checkbox yields the truthy string `'workflow_dispatch'` and silently forces a
> dry run. The reconcile then never copies anything. The form above relies on
> `inputs.dry_run` being null (falsy) on non-dispatch events, which is correct.

PRs from forks don't receive repository secrets. The login step then fails with the `JFROG_USERNAME is empty` error described below, even for a dry run.

## `images.yaml` fields

At the root of the file:

| Field | Required | Notes |
| --- | --- | --- |
| `target_repo` | yes | Artifactory repo key every image is published to |
| `projects` | yes | One or more source projects |

Per entry in `projects:`:

| Field | Required | Notes |
| --- | --- | --- |
| `gcp_wif_provider` | yes | Workload Identity Provider resource name |
| `gcp_sa_email` | yes | Service account to impersonate |
| `images` | yes | See the three forms above |
| `gcp_project` | for shorthand | Also inferred from an explicit `source_image` |
| `gar_repo` | for shorthand | GAR repository holding the images |
| `gar_registry` | for shorthand | Registry host, `<location>-docker.pkg.dev`. No default, because the region is never assumed |

Per image, after expansion, three fields are required:

- `source_image`: host and path, **no tag**
- `target_path`: unique per tag across the file
- `tag`: immutable, and `latest` is rejected

Shorthand supplies the first two.

## Workflow inputs and secrets

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `images_file` | no | `images.yaml` | Path **in the calling repo** |
| `artifactory_registry` | no | `beelzi.jfrog.io` | Destination registry host |
| `dry_run` | no | `false` | Resolve digests and report, copy nothing |
| `artifactory_path_style` | no | `true` | Prepend the repo key instead of subdomain routing |
| `action_ref` | no | `main` | Ref of this repo. Pin to a tag in production |

| Secret | Required | Description |
| --- | --- | --- |
| `JFROG_USERNAME` | yes | Artifactory user |
| `JFROG_TOKEN` | yes | Access token with deploy rights on `target_repo` |

`artifactory_path_style` defaults to `true` because the free JFrog instance has no per-repo subdomains.

The composite action at `actions/jfrog-images-publish` takes the same values flattened, one project per call. The credentials are passed as the inputs `artifactory_username` and `artifactory_token`, because composite actions can't read the `secrets` context. Call the workflow unless you need the step directly.

## Job summary

The run report records activity in a common shape, one section per project and one row per image:

```text
### my-gcp-project → infosec-local

2 copied · 1 already correct · 1 failed

| image | outcome | digest |
| --- | --- | --- |
| `my-app:cc1546d2` | copied (new)    | `9f2a1c0b4de8` |
| `my-agent:v1`     | already correct | `3c0d81ea77b2` |
```

Possible outcomes:

- `copied (new)` and `copied (digest mismatch)`
- `already correct`
- `would copy (…)`, under `dry_run`
- `unreadable at source`, `copy failed` and `verify failed`

## Notes

- Public images (`docker.io`, `quay.io`, …) must not be listed. Artifactory remote repos proxy them on demand. This is enforced in review.
- Pull through the virtual repo: `docker-virtual.myorg.jfrog.io/<target_path>:<tag>`. When a path exists in both a local and a remote repo behind the virtual, the resolution order decides which one you get.
- Scope the WIF provider to `attribute.repository`. A pool that trusts the whole org lets any repo read your registry.
- Scope the JFrog token to deploy on `target_repo` only, and rotate it by updating the repo secret. Nothing else needs to change.
- Pin `action_ref` to a version tag in production.

## Troubleshooting

**`no OIDC token available`**: the calling repository is missing
`permissions: id-token: write` at the **workflow level**. A job that is only
`uses: <reusable workflow>` runs with the permissions its caller granted at
workflow level. A `permissions:` block on that job can restrict them but can't
add `id-token`. Putting the grant only on the job is the usual cause. The run
then logs just `Contents: read` / `Metadata: read`, and `OIDC_URL` stays empty.

**`JFROG_USERNAME is empty` / `JFROG_TOKEN is empty`**: the repo secret is
missing, or the caller has no `secrets:` block (or `secrets: inherit`). An unset
secret reaches the workflow as an empty string, not an error.

**`401` / `403` from Artifactory**: the token has expired, or it lacks deploy
rights on `target_repo`. It is also possible that `JFROG_USERNAME` isn't the
user the token was issued to.

**`images file not found`**: `images_file` doesn't exist in the calling repo at
the commit being run.

**`both publish 'x:tag'`**: two entries map to the same destination. Rename one
with an explicit `target_path`, or remove the duplicate.

**`spans multiple source registries`**: one `projects:` entry mixes two registry hosts. Split it into one entry per host, since each authenticates separately.

**``needs `gar_registry` on the project``**: a shorthand image has no registry to resolve against. Add the host your GAR repo lives in, or give that image an explicit `source_image`.

**`tag moved at source`**: the source tag was re-pushed. The copy proceeds, but tags should be immutable, so investigate the source.
