"""Publish the three Consent Manager images to the OAN ECR. Only the develop branch build runs this.

    python3 ci/images/publish_images.py <manifest.json>

Reads the images ci/images/images.sh built (oan-ci/<image>:<tag>) and fails closed, before anything is pushed, unless:
the build is `develop` and not a pull request; the tag is the one init.yaml, the branch and the build number give;
the work tree is clean and at SOURCE_COMMIT; every local image carries OCI source and revision labels of that commit;
the region is ap-south-1 and the credential belongs to the OAN ECR account; every repository exists with the naming
standard's settings (created when missing); and no tag exists yet in any of the three repositories. Then it pushes,
reads each tag back from the registry, requires the registry's digest to be the one the push reported and writes the
manifest: exact <repository>:<tag>@<digest>, the OCI source and revision, the build and what happens next. The
credential comes from the Jenkins binding (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY) and goes to Docker on stdin only.

It never deploys and never changes a Helm chart. Standard library only.
"""
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ecr_repository as ecr  # noqa: E402
import image_names  # noqa: E402

REGION = ecr.REGION
# The OAN ECR account (the registry host of every image in oan-helm). A credential of another account is refused.
EXPECTED_ACCOUNT = "379220350808"
SOURCE = "https://github.com/Centre-for-Open-Societal-Systems/consent-manager"
COMMIT = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
PUSHED = re.compile(r"digest:\s*(sha256:[0-9a-f]{64})")
HANDOFF = ("Not deployed and no chart changed. Next: install these digests on registry-test, then a separate "
           "oan-helm promotion that pins them.")


class Refused(Exception):
    pass


def masked(text):
    """Error text with anything token-like replaced: errors are printed, secrets never are."""
    return re.sub(r"[A-Za-z0-9+/_=-]{32,}", "***", text or "")


def command(runner, argv, stdin=None):
    code, out, err = runner(argv, stdin)
    if code:
        lines = masked(err).strip().splitlines()
        raise Refused("%s failed: %s" % (" ".join(argv[:2]), lines[-1][:200] if lines else "no message"))
    return out


def preconditions(env, runner):
    """The facts a publication depends on, or Refused. Returns (tag, commit)."""
    if env.get("BRANCH_NAME") != "develop" or env.get("CHANGE_ID") or env.get("CHANGE_TARGET"):
        raise Refused("only a develop branch build publishes; this is %r%s" % (
            env.get("BRANCH_NAME"), " (pull request)" if env.get("CHANGE_ID") else ""))
    if env.get("AWS_REGION") != REGION:
        raise Refused("AWS_REGION must be %s" % REGION)
    try:
        expected = image_names.tag(env.get("BRANCH_NAME"), env.get("BUILD_NUMBER", ""))
    except ValueError as error:
        raise Refused(str(error))
    if env.get("IMAGE_TAG") != expected or not image_names.is_tag(expected):
        raise Refused("IMAGE_TAG %r is not %s, the tag init.yaml, the branch and the build give" % (env.get("IMAGE_TAG"), expected))
    commit = env.get("SOURCE_COMMIT", "")
    if not COMMIT.fullmatch(commit):
        raise Refused("SOURCE_COMMIT is not a 40 character commit")
    if command(runner, ["git", "rev-parse", "--verify", "HEAD"]).strip() != commit:
        raise Refused("the checkout is not at SOURCE_COMMIT")
    if command(runner, ["git", "status", "--porcelain", "--untracked-files=no"]).strip():
        raise Refused("the work tree has changes: the image would not be this source")
    return expected, commit


def local_labels(runner, name, tag, commit):
    """The OCI labels of a built image; Refused unless source and revision are this repository and commit."""
    out = command(runner, ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", "oan-ci/%s:%s" % (name, tag)])
    try:
        labels = json.loads(out) or {}
    except ValueError:
        raise Refused("unreadable labels of oan-ci/%s:%s" % (name, tag))
    if labels.get("org.opencontainers.image.source") != SOURCE or labels.get("org.opencontainers.image.revision") != commit:
        raise Refused("%s does not carry the OCI source and revision of %s" % (name, commit))
    return {"source": SOURCE, "revision": commit}


def account_of(runner):
    out = command(runner, ["aws", "sts", "get-caller-identity", "--region", REGION, "--query", "Account", "--output", "text"]).strip()
    if not re.fullmatch(r"\d{12}", out):
        raise Refused("the caller identity is not an AWS account id")
    if out != EXPECTED_ACCOUNT:
        raise Refused("the credential belongs to account %s, not the OAN ECR account" % out)
    return out


def lookup(call, repository, tag):
    """The registry's (digest, registryId) of a tag, or None when the tag does not exist."""
    try:
        out = call(["describe-images", "--repository-name", repository, "--image-ids", "imageTag=%s" % tag, "--output", "json"])
    except ecr.Missing:
        return None
    try:
        details = json.loads(out)["imageDetails"]
    except (ValueError, KeyError):
        raise Refused("unreadable lookup of %s:%s" % (repository, tag))
    if len(details) != 1 or tag not in (details[0].get("imageTags") or []) or not DIGEST.fullmatch(details[0].get("imageDigest", "")):
        raise Refused("the registry answered with another image for %s:%s" % (repository, tag))
    return details[0]["imageDigest"], details[0].get("registryId", "")


def publish(env, runner, manifest_path):
    tag, commit = preconditions(env, runner)
    account = account_of(runner)
    registry = "%s.dkr.ecr.%s.amazonaws.com" % (account, REGION)

    def call(args):
        return ecr.aws(args, runner)

    names = list(image_names.IMAGES)
    labels = {name: local_labels(runner, name, tag, commit) for name in names}
    repositories = {name: image_names.repository(name) for name in names}
    for name in names:
        print("%s: %s" % (repositories[name], ecr.ensure(repositories[name], image_names.legacy_repository(name), call)))
    # Every tag is checked before the first push: a collision never leaves a partial set behind.
    for name in names:
        if lookup(call, repositories[name], tag) is not None:
            raise Refused("tag collision: %s:%s already exists and tags are never replaced; bump init.yaml" % (repositories[name], tag))
    password = command(runner, ["aws", "ecr", "get-login-password", "--region", REGION])
    command(runner, ["docker", "login", "--username", "AWS", "--password-stdin", registry], password)
    images = {}
    for name in names:
        reference = "%s/%s:%s" % (registry, repositories[name], tag)
        command(runner, ["docker", "tag", "oan-ci/%s:%s" % (name, tag), reference])
        pushed = PUSHED.findall(command(runner, ["docker", "push", reference]))
        if not pushed:
            raise Refused("the push of %s reported no digest" % reference)
        found = lookup(call, repositories[name], tag)
        if found is None or found[0] != pushed[-1] or found[1] != account:
            raise Refused("the registry does not hold %s at the pushed digest in this account" % reference)
        images[name] = {"repository": repositories[name], "tag": tag, "digest": found[0],
                        "reference": "%s/%s:%s@%s" % (registry, repositories[name], tag, found[0]),
                        "ociSource": labels[name]["source"], "ociRevision": labels[name]["revision"]}
    result = {"component": "consent-manager", "sourceRepository": SOURCE, "sourceCommit": commit,
              "version": image_names.chart_version(), "tag": tag, "build": env["BUILD_NUMBER"], "branch": "develop",
              "registry": registry, "images": images, "verification": "tag-read-back-from-registry-at-pushed-digest",
              "deployment": "not-attempted", "helm": "not-changed", "handoff": HANDOFF}
    out = Path(manifest_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    out.with_suffix(".tsv").write_text(
        "".join("%s\t%s\t%s\n" % (n, i["reference"], i["digest"]) for n, i in images.items()), encoding="utf-8")
    return result


def main(argv, env=None, runner=ecr.run):
    if len(argv) != 2:
        print("usage: publish_images.py <manifest.json>", file=sys.stderr)
        return 1
    env = dict(os.environ if env is None else env)
    # Docker's credential store lives in a directory of its own, removed afterwards.
    config = tempfile.mkdtemp(prefix="oan-docker-")
    os.environ["DOCKER_CONFIG"] = config
    try:
        result = publish(env, runner, argv[1])
        for image in result["images"].values():
            print("published %s" % image["reference"])
        return 0
    except (Refused, RuntimeError, OSError, KeyError) as error:
        print("FAIL: %s" % masked(str(error)), file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(config, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
