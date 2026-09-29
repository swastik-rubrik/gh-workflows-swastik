#!/usr/bin/env python3
"""Build the publish matrix from images.yaml.

One matrix entry per project. The file declares its `target_repo` once at the
root and lists one or more `projects:`, each with its own GCP credentials and
images -- so one file can pull images from several GCP projects, all landing in
the same Artifactory repo.

Usage:
    build-matrix.py --file images.yaml

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

REQUIRED = ("target_repo", "projects")
REQUIRED_PROJECT = ("gcp_wif_provider", "gcp_sa_email")
REQUIRED_IMAGE = ("source_image", "target_path", "tag")

# There is deliberately no default GAR region. A default would live here, in
# the shared repo, where the repo whose images it resolves cannot see or change
# it -- and getting it wrong is silent: the run builds a plausible path in the
# wrong region and fails later at pull time. Shorthand states its registry.


def expand_images(at, proj):
    """Expand the shorthand image forms into full {source_image, target_path, tag}.

    Three forms are accepted, and a project may mix them:

        images:
          - mock-react:v1                       # shorthand, needs gar_repo
          - name: mock-react                    # shorthand, split fields
            tag: v1
          - source_image: <host>/<proj>/<repo>/mock-react   # explicit, always valid
            target_path: mock-react
            tag: v1

    Runs before validate_images, so the `latest` and tagged-source_image guards
    apply to every form.
    """
    gar_repo = proj.get("gar_repo")
    gcp_project = proj.get("gcp_project")
    registry = proj.get("gar_registry")

    out = []
    for i, img in enumerate(proj.get("images") or []):
        where = "%s: images[%d]" % (at, i)

        if isinstance(img, str):
            # "mock-react:v1" -- exactly one colon, both halves non-empty.
            name, sep, tag = img.partition(":")
            if not sep or not name or not tag:
                sys.exit("%s: %r must be in the form 'name:tag'" % (where, img))
            img = {"name": name, "tag": tag}
        elif not isinstance(img, dict):
            sys.exit("%s: expected a mapping or 'name:tag' string, got %s"
                     % (where, type(img).__name__))
        else:
            img = dict(img)

        if not img.get("source_image"):
            name = img.pop("name", None)
            if not name:
                sys.exit("%s: needs either `source_image` or `name`" % where)
            if not gar_repo:
                sys.exit("%s: shorthand image %r needs `gar_repo` on the project "
                         "(or give the image an explicit `source_image`)" % (where, name))
            if not gcp_project:
                sys.exit("%s: shorthand image %r needs `gcp_project` on the project "
                         "(or give the image an explicit `source_image`)" % (where, name))
            if not registry:
                sys.exit(
                    "%s: shorthand image %r needs `gar_registry` on the project, "
                    "e.g.\n"
                    "    gar_registry: asia-south1-docker.pkg.dev\n"
                    "It is the host of your Artifact Registry -- "
                    "`<location>-docker.pkg.dev`, where <location> is the region "
                    "the repo was created in (see the Artifact Registry console, "
                    "or `gcloud artifacts repositories list`). There is no default: "
                    "guessing the region would build a valid-looking path that only "
                    "fails when the image is pulled."
                    % (where, name))
            img["source_image"] = "%s/%s/%s/%s" % (registry, gcp_project, gar_repo, name)
            img.setdefault("target_path", name)
        else:
            img.setdefault("target_path",
                           str(img["source_image"]).rstrip("/").split("/")[-1])

        out.append(img)

    return out


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


def check_unique_targets(path, projects):
    """Every image lands in the one target_repo, so no two may share a
    target_path:tag. Two sources for one destination would overwrite each other
    on every run, each job seeing the other's digest as a mismatch.
    """
    seen = {}
    for p, proj in enumerate(projects):
        for i, img in enumerate(proj["images"]):
            key = "%s:%s" % (img["target_path"], img["tag"])
            at = "projects[%d].images[%d]" % (p, i)
            if key in seen:
                sys.exit("%s: %s and %s both publish %r; each destination "
                         "needs exactly one source" % (path, seen[key], at, key))
            seen[key] = at


def load_spec(path):
    """Load and validate the images file."""
    if not os.path.isfile(path):
        sys.exit("images file not found: %s" % path)

    # A syntax error is a typo in the calling repo, so report the file and the
    # position PyYAML found rather than letting a traceback reach the job log.
    try:
        with open(path, encoding="utf-8") as fh:
            spec = yaml.safe_load(fh.read())
    except yaml.YAMLError as exc:
        detail = getattr(exc, "problem", None) or str(exc)
        mark = getattr(exc, "problem_mark", None)
        where = " (line %d, column %d)" % (mark.line + 1, mark.column + 1) if mark else ""
        sys.exit("%s: invalid YAML%s: %s" % (path, where, detail))

    if not isinstance(spec, dict):
        sys.exit("%s: expected a mapping at the top level" % path)

    missing = [k for k in REQUIRED if not spec.get(k)]
    if missing:
        sys.exit("%s: missing required field(s): %s" % (path, ", ".join(missing)))

    projects = spec["projects"]
    if not isinstance(projects, list):
        sys.exit("%s: `projects` must be a non-empty list" % path)
    for i, proj in enumerate(projects):
        at = "%s: projects[%d]" % (path, i)
        if not isinstance(proj, dict):
            sys.exit("%s: expected a mapping" % at)
        missing = [k for k in REQUIRED_PROJECT if not proj.get(k)]
        if missing:
            sys.exit("%s: missing required field(s): %s" % (at, ", ".join(missing)))
        if not proj.get("images"):
            sys.exit("%s: no images declared" % at)
        # Shorthand entries become full records here, so everything downstream
        # -- validation, the matrix, publish.sh -- sees one shape only.
        proj["images"] = expand_images(at, proj)
        validate_images(at, proj["images"])

    check_unique_targets(path, projects)
    return spec


def gcp_project_of(at, proj):
    """Explicit gcp_project, else infer from the first image's source host path."""
    if proj.get("gcp_project"):
        return proj["gcp_project"]
    src = proj["images"][0].get("source_image", "")
    parts = str(src).split("/")
    # <location>-docker.pkg.dev/<project>/<repo>/<path...>
    if len(parts) >= 2 and ".pkg.dev" in parts[0]:
        return parts[1]
    sys.exit("%s: cannot infer gcp_project from %r; set it explicitly" % (at, src))


def source_registry_of(at, gcp_project, imgs):
    """The registry host to log in to, taken from the images themselves.
    """
    hosts = sorted({img["source"].split("/")[0] for img in imgs})
    if len(hosts) > 1:
        sys.exit("%s: project %r spans multiple source registries (%s); "
                 "split them into separate `projects:` entries"
                 % (at, gcp_project, ", ".join(hosts)))
    return hosts[0]


def build_entries(spec, path):
    """The images file in, one matrix entry per project out."""
    entries = []

    for p, proj in enumerate(spec["projects"]):
        at = "%s: projects[%d]" % (path, p)
        imgs = [{
            "source": "%s:%s" % (img["source_image"], img["tag"]),
            "target_path": img["target_path"],
            "tag": img["tag"],
        } for img in proj["images"]]

        gcp_project = gcp_project_of(at, proj)
        entries.append({
            "name": gcp_project,
            "gcp_project": gcp_project,
            "source_registry": source_registry_of(at, gcp_project, imgs),
            "wif_provider": proj["gcp_wif_provider"],
            "gcp_sa_email": proj["gcp_sa_email"],
            "target_repo": spec["target_repo"],
            "images": imgs,
        })

    return entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True,
                    help="Path to images.yaml in the calling repository")
    args = ap.parse_args()

    spec = load_spec(args.file)
    include = build_entries(spec, args.file)
    matrix = {"include": include}

    # Human-readable summary goes to stderr so stdout stays pure JSON.
    total = sum(len(e["images"]) for e in include)
    print("matrix: %d job(s), %d image(s) -> %s"
          % (len(include), total, spec["target_repo"]), file=sys.stderr)
    for e in include:
        print("  %-40s %d image(s)" % (e["name"], len(e["images"])), file=sys.stderr)

    print(json.dumps(matrix, separators=(",", ":")))


if __name__ == "__main__":
    main()
