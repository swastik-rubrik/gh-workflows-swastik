#!/usr/bin/env python3
"""Build the publish matrix for ONE team, from teams/<team>.yaml.

A run reconciles exactly one team. That is not an ergonomic choice, it is what
the credential model allows: the Vault JWT role is scoped to a single team, so a
run holds one team's Artifactory token and can only push to that team's
local-repo. Fanning out over several teams in one run would mean either one role
reading several teams' paths -- which is the shared credential we removed -- or
a run holding credentials it has no business holding.

So there is deliberately no --all and no directory-wide mode. `teams/` stays a
directory for review and audit; a run addresses one file in it.

The output is still a matrix (a one-entry `include` list) rather than a flat
object, so the calling workflow keeps `strategy.matrix` and the per-entry job
naming unchanged.

Reads YAML with a tiny hand-rolled parser so the workflow needs no pip install and
no yq. The team files are a fixed, flat shape; see parse_team_yaml.

Usage:
    build-matrix.py --teams-dir DIR --team NAME [--environment ENV]

Which images actually get copied is decided per-image at runtime by comparing
registry digests; this script only decides scope.

Prints the matrix JSON to stdout and a human summary to stderr, so the caller
can capture one without the other.
"""

import argparse
import json
import os
import sys


def _strip_comment(line):
    """Remove a trailing # comment that is not inside quotes."""
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def _scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_scalar(x) for x in inner.split(",")]
    return v


def parse_team_yaml(text):
    """Parse the team file shape: top-level scalars plus an `images:` list of maps."""
    data, images = {}, []
    cur = None
    in_images = False

    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line.strip():
            continue

        indent = len(line) - len(line.lstrip())
        body = line.strip()

        if indent == 0:
            if body == "images:":
                in_images, cur = True, None
                continue
            in_images = False
            if ":" in body:
                k, _, v = body.partition(":")
                data[k.strip()] = _scalar(v)
            continue

        if not in_images:
            continue

        if body.startswith("- "):
            cur = {}
            images.append(cur)
            body = body[2:].strip()

        if cur is not None and ":" in body:
            k, _, v = body.partition(":")
            cur[k.strip()] = _scalar(v)

    data["images"] = images
    return data

REQUIRED = ("team", "target_repo", "gcp_wif_provider", "gcp_sa_email")
REQUIRED_IMAGE = ("source_image", "target_path", "tag")


def validate_images(path, spec):
    for i, img in enumerate(spec["images"]):
        where = "%s: images[%d]" % (path, i)
        missing = [k for k in REQUIRED_IMAGE if not img.get(k)]
        if missing:
            sys.exit("%s: missing required field(s): %s" % (where, ", ".join(missing)))
        # A copy must be reproducible; a floating tag makes the digest
        # comparison meaningless because the source moves under it.
        if str(img["tag"]).strip().lower() == "latest":
            sys.exit("%s: tag 'latest' is not allowed; pin an immutable tag" % where)
        if ":" in img["source_image"]:
            sys.exit("%s: source_image must not carry a tag; use the `tag` field" % where)


def available_teams(teams_dir):
    return sorted(
        os.path.splitext(n)[0]
        for n in os.listdir(teams_dir)
        if n.endswith((".yaml", ".yml"))
    )


def load_team(teams_dir, team):
    """Load exactly one team file.
    """
    if not os.path.isdir(teams_dir):
        sys.exit("teams dir not found: %s" % teams_dir)

    names = available_teams(teams_dir)
    if not names:
        sys.exit("no team files found in %s" % teams_dir)
    if team not in names:
        sys.exit("no team file for %r in %s; available: %s"
                 % (team, teams_dir, ", ".join(names)))

    for ext in (".yaml", ".yml"):
        path = os.path.join(teams_dir, team + ext)
        if os.path.isfile(path):
            break

    with open(path, encoding="utf-8") as fh:
        spec = parse_team_yaml(fh.read())

    missing = [k for k in REQUIRED if not spec.get(k)]
    if missing:
        sys.exit("%s: missing required field(s): %s" % (path, ", ".join(missing)))
    if not spec["images"]:
        sys.exit("%s: no images declared" % path)
    # The Vault role is derived from the `team:` field, while the run was
    # authorised against the filename.
    if spec["team"] != team:
        sys.exit("%s: declares team %r but the file is named %r; they must match"
                 % (path, spec["team"], team))
    validate_images(path, spec)
    return spec


VAULT_PATH_TEMPLATE = "rubrik-secret/data/infosec/{env}/{team}/jfrog-artifactory"

def vault_path_of(spec, environment):
    """Explicit vault_path wins; otherwise build it from the standard layout.
    """
    if spec.get("vault_path"):
        return spec["vault_path"]
    return VAULT_PATH_TEMPLATE.format(env=environment, team=spec["team"])


def vault_role_of(spec):
    """Vault JWT role for this team(eg: appsec,infosec-sre). Defaults to the team name.
    """
    return spec.get("vault_role") or spec["team"]


def gcp_project_of(spec):
    """Explicit gcp_project, else infer from the first image's source host path."""
    if spec.get("gcp_project"):
        return spec["gcp_project"]
    src = spec["images"][0].get("source_image", "")
    parts = src.split("/")
    # <location>-docker.pkg.dev/<project>/<repo>/<path...>
    if len(parts) >= 2 and ".pkg.dev" in parts[0]:
        return parts[1]
    sys.exit("team %r: cannot infer gcp_project from %r; set it explicitly"
             % (spec.get("team"), src))


def source_registry_of(spec, imgs):
    """The registry host to log in to, taken from the images themselves.
    """
    hosts = sorted({img["source"].split("/")[0] for img in imgs})
    if len(hosts) > 1:
        sys.exit("team %r: images span multiple source registries (%s); "
                 "split them into separate team files"
                 % (spec.get("team"), ", ".join(hosts)))
    return hosts[0]


def build(spec, team, environment):
    """One team in, at most one matrix entry out.

    An entry is emitted only if the team has images for this environment; an
    empty `include` is a valid, successful outcome and the workflow skips the
    publish job.
    """
    imgs = []
    for img in spec["images"]:
        envs = img.get("environments") or []
        if isinstance(envs, str):
            envs = [envs]
        # absent or empty == every environment
        if envs and environment not in envs:
            continue
        imgs.append({
            "source": "%s:%s" % (img["source_image"], img["tag"]),
            "target_path": img["target_path"],
            "tag": img["tag"],
        })

    if not imgs:
        return {"include": []}

    return {"include": [{
        "name": "%s/%s" % (gcp_project_of(spec), spec["team"]),
        "team": spec["team"],
        "file": team,
        "gcp_project": gcp_project_of(spec),
        "source_registry": source_registry_of(spec, imgs),
        "wif_provider": spec["gcp_wif_provider"],
        "gcp_sa_email": spec["gcp_sa_email"],
        "vault_path": vault_path_of(spec, environment),
        "vault_role": vault_role_of(spec),
        "target_repo": spec["target_repo"],
        "images": imgs,
    }]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teams-dir", required=True)
    ap.add_argument("--team", required=True,
                    help="Team file to reconcile, without the extension. "
                         "Exactly one; a run is scoped to a single Vault role.")
    ap.add_argument("--environment", default="prod")
    args = ap.parse_args()

    spec = load_team(args.teams_dir, args.team)
    matrix = build(spec, args.team, args.environment)

    # Human-readable summary goes to stderr so stdout stays pure JSON.
    total = sum(len(e["images"]) for e in matrix["include"])
    print("matrix: %d job(s), %d image(s), environment=%s"
          % (len(matrix["include"]), total, args.environment), file=sys.stderr)
    for e in matrix["include"]:
        print("  %-40s %d image(s)" % (e["name"], len(e["images"])), file=sys.stderr)

    print(json.dumps(matrix, separators=(",", ":")))


if __name__ == "__main__":
    main()
