#!/usr/bin/env python3
"""Build the publish matrix from teams/*.yaml.

One matrix entry per (team, project). A team file declares its identity once at
the root and lists one or more `projects:`, each with its own GCP credentials and
images -- so a single team can pull images from several GCP projects.

The Vault JWT role is scoped to a team, so every entry from one file shares that
team's Artifactory token and can only push to that team's local-repo.

Usage:
    build-matrix.py --teams-dir DIR [--team NAME] [--environment ENV]

Which images actually get copied is decided per-image at runtime by comparing
registry digests; this script only decides scope.

Prints the matrix JSON to stdout and a human summary to stderr, so the caller
can capture one without the other.
"""

import argparse
import json
import os
import sys
import yaml

REQUIRED = ("team", "target_repo")
REQUIRED_PROJECT = ("gcp_wif_provider", "gcp_sa_email")
REQUIRED_IMAGE = ("source_image", "target_path", "tag")

def validate_images(where, images):
    for i, img in enumerate(images):
        at = "%s: images[%d]" % (where, i)
        if not isinstance(img, dict):
            sys.exit("%s: expected a mapping, got %s" % (at, type(img).__name__))
        missing = [k for k in REQUIRED_IMAGE if not img.get(k)]
        if missing:
            sys.exit("%s: missing required field(s): %s" % (at, ", ".join(missing)))
        # A copy must be reproducible; a floating tag makes the digest
        # comparison meaningless because the source moves under it.
        if str(img["tag"]).strip().lower() == "latest":
            sys.exit("%s: tag 'latest' is not allowed; pin an immutable tag" % at)
        if ":" in str(img["source_image"]):
            sys.exit("%s: source_image must not carry a tag; use the `tag` field" % at)


def available_teams(teams_dir):
    return sorted(
        os.path.splitext(n)[0]
        for n in os.listdir(teams_dir)
        if n.endswith((".yaml", ".yml"))
    )


def team_path(teams_dir, team):
    for ext in (".yaml", ".yml"):
        path = os.path.join(teams_dir, team + ext)
        if os.path.isfile(path):
            return path
    sys.exit("no team file for %r in %s" % (team, teams_dir))


def load_team(teams_dir, team):
    """Load and validate one team file. Returns None if the file is empty.
    """
    path = team_path(teams_dir, team)

    # A syntax error is a team's own typo, so report the file and the position
    # PyYAML found rather than letting a traceback reach the job log.
    try:
        with open(path, encoding="utf-8") as fh:
            spec = yaml.safe_load(fh.read())
    except yaml.YAMLError as exc:
        detail = getattr(exc, "problem", None) or str(exc)
        mark = getattr(exc, "problem_mark", None)
        where = " (line %d, column %d)" % (mark.line + 1, mark.column + 1) if mark else ""
        sys.exit("%s: invalid YAML%s: %s" % (path, where, detail))

    if spec is None:
        return None
    if not isinstance(spec, dict):
        sys.exit("%s: expected a mapping at the top level" % path)

    missing = [k for k in REQUIRED if not spec.get(k)]
    if missing:
        sys.exit("%s: missing required field(s): %s" % (path, ", ".join(missing)))

    # The Vault role is derived from the `team:` field, while the run was
    # authorised against the filename. 
    if spec["team"] != team:
        sys.exit("%s: declares team %r but the file is named %r; they must match"
                 % (path, spec["team"], team))

    projects = normalise_projects(path, spec)
    for i, proj in enumerate(projects):
        at = "%s: projects[%d]" % (path, i)
        missing = [k for k in REQUIRED_PROJECT if not proj.get(k)]
        if missing:
            sys.exit("%s: missing required field(s): %s" % (at, ", ".join(missing)))
        if not proj.get("images"):
            sys.exit("%s: no images declared" % at)
        validate_images(at, proj["images"])

    spec["projects"] = projects
    return spec


def normalise_projects(path, spec):
    """Return the file's projects as a list.
    """
    projects = spec.get("projects")

    if projects is None:
        if not spec.get("images"):
            sys.exit("%s: no projects and no images declared" % path)
        return [{
            "gcp_project": spec.get("gcp_project"),
            "gcp_wif_provider": spec.get("gcp_wif_provider"),
            "gcp_sa_email": spec.get("gcp_sa_email"),
            "images": spec["images"],
        }]

    if not isinstance(projects, list) or not projects:
        sys.exit("%s: `projects` must be a non-empty list" % path)
    if spec.get("images"):
        sys.exit("%s: declares both `projects` and a root-level `images`; "
                 "move the images under a project" % path)
    for proj in projects:
        if not isinstance(proj, dict):
            sys.exit("%s: each project must be a mapping" % path)
    return projects

VAULT_PATH_TEMPLATE = "rubrik-secret/data/infosec/{env}/{team}/jfrog-artifactory"

def vault_path_of(spec, environment):
    """Explicit vault_path wins; otherwise build it from the standard layout."""
    if spec.get("vault_path"):
        return spec["vault_path"]
    return VAULT_PATH_TEMPLATE.format(env=environment, team=spec["team"])

def vault_role_of(spec):
    """Vault JWT role for this team (eg: appsec, infosec-sre). Defaults to the team name."""
    return spec.get("vault_role") or spec["team"]


def gcp_project_of(path, proj):
    """Explicit gcp_project, else infer from the first image's source host path."""
    if proj.get("gcp_project"):
        return proj["gcp_project"]
    src = proj["images"][0].get("source_image", "")
    parts = str(src).split("/")
    # <location>-docker.pkg.dev/<project>/<repo>/<path...>
    if len(parts) >= 2 and ".pkg.dev" in parts[0]:
        return parts[1]
    sys.exit("%s: cannot infer gcp_project from %r; set it explicitly" % (path, src))


def source_registry_of(path, gcp_project, imgs):
    """The registry host to log in to, taken from the images themselves.
    """
    hosts = sorted({img["source"].split("/")[0] for img in imgs})
    if len(hosts) > 1:
        sys.exit("%s: project %r spans multiple source registries (%s); "
                 "split them into separate `projects:` entries"
                 % (path, gcp_project, ", ".join(hosts)))
    return hosts[0]


def build_entries(spec, team, path):
    """One team file in, one matrix entry per project out.

    A project with no images yields no entry; an empty `include` overall is a
    valid, successful outcome and the workflow skips the publish job.
    """
    entries = []

    for proj in spec["projects"]:
        imgs = [{
            "source": "%s:%s" % (img["source_image"], img["tag"]),
            "target_path": img["target_path"],
            "tag": img["tag"],
        } for img in proj["images"]]

        if not imgs:
            continue

        gcp_project = gcp_project_of(path, proj)
        entries.append({
            "name": "%s/%s" % (gcp_project, spec["team"]),
            "team": spec["team"],
            "file": team,
            "gcp_project": gcp_project,
            "source_registry": source_registry_of(path, gcp_project, imgs),
            "wif_provider": proj["gcp_wif_provider"],
            "gcp_sa_email": proj["gcp_sa_email"],
            "target_repo": spec["target_repo"],
            "images": imgs,
        })

    return entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teams-dir", required=True)
    ap.add_argument("--team", default=None,
                    help="Team file to reconcile, without the extension. "
                         "Omit to fan out over every team file in the directory.")
    ap.add_argument("--environment", default="prod")
    args = ap.parse_args()

    if not os.path.isdir(args.teams_dir):
        sys.exit("teams dir not found: %s" % args.teams_dir)

    names = available_teams(args.teams_dir)
    if not names:
        sys.exit("no team files found in %s" % args.teams_dir)

    if args.team is not None:
        if args.team not in names:
            sys.exit("no team file for %r in %s; available: %s"
                     % (args.team, args.teams_dir, ", ".join(names)))
        names = [args.team]

    include, skipped = [], []
    for name in names:
        spec = load_team(args.teams_dir, name)
        if spec is None:
            skipped.append(name)
            continue
        entries = build_entries(spec, name, team_path(args.teams_dir, name))
        for entry in entries:
            entry["vault_path"] = vault_path_of(spec, args.environment)
            entry["vault_role"] = vault_role_of(spec)
        include.extend(entries)

    matrix = {"include": include}

    # Human-readable summary goes to stderr so stdout stays pure JSON.
    total = sum(len(e["images"]) for e in include)
    print("matrix: %d job(s), %d image(s), environment=%s"
          % (len(include), total, args.environment), file=sys.stderr)
    for e in include:
        print("  %-40s %d image(s)" % (e["name"], len(e["images"])), file=sys.stderr)
    if skipped:
        print("  skipped (empty): %s" % ", ".join(skipped), file=sys.stderr)

    print(json.dumps(matrix, separators=(",", ":")))


if __name__ == "__main__":
    main()
