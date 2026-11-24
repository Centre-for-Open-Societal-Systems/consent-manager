"""Create a missing ECR repository for a Consent Manager image the way the Farmer ones are made, and refuse one that
does not meet the standard. Run by the develop pipeline.

    python3 ci/images/ecr_repository.py ensure <repository> <template repository>

A missing repository is created with the naming standard's settings (oan-helm docs/release/naming-standard.md,
section 6): immutable tags, scan on push, AES256 encryption and the tags owner=consent-manager and
naming=eoan-standard. It also gets the lifecycle policy of the template (the image's Commons repository before eoan)
when that has one. Nothing is created by hand.

Unlike the Farmer script, an existing repository is also read back: mutable tags, no scan on push or another
encryption fail the build, because a mutable repository would let a published tag be replaced.

Needs the aws CLI with the pipeline's ECR credential. Standard library only. Exit codes: 0 usable, 1 failure.
"""
import json
import subprocess
import sys

REGION = "ap-south-1"
SETTINGS = ["--image-tag-mutability", "IMMUTABLE", "--image-scanning-configuration", "scanOnPush=true",
            "--encryption-configuration", "encryptionType=AES256",
            "--tags", "Key=owner,Value=consent-manager", "Key=naming,Value=eoan-standard"]


class Missing(Exception):
    pass


def run(argv, stdin=None):
    """(returncode, stdout, stderr) of a command; the one place that starts a process, so tests replace it."""
    result = subprocess.run(argv, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    return result.returncode, result.stdout, result.stderr


def aws(args, runner=run):
    """stdout of `aws ecr <args>`; Missing for a repository, image or lifecycle policy that does not exist.

    Every other failure (credentials, throttling, network, access) raises: nothing is assumed to be missing.
    """
    code, out, err = runner(["aws", "ecr", "--region", REGION] + args)
    if code:
        for marker in ("RepositoryNotFoundException", "LifecyclePolicyNotFoundException", "ImageNotFoundException"):
            if marker in err:
                raise Missing(marker)
        lines = err.strip().splitlines()
        raise RuntimeError("aws ecr %s failed: %s" % (args[0], lines[-1][:200] if lines else "no message"))
    return out


def check_settings(name, call):
    """The repository's settings from the registry, refused unless they are the standard's. Returns its URI."""
    try:
        repositories = json.loads(call(["describe-repositories", "--repository-names", name, "--output", "json"]))["repositories"]
    except (ValueError, KeyError) as error:
        raise RuntimeError("unreadable description of %s" % name) from error
    if len(repositories) != 1 or repositories[0].get("repositoryName") != name:
        raise RuntimeError("the registry described another repository than %s" % name)
    found = repositories[0]
    problems = []
    if found.get("imageTagMutability") != "IMMUTABLE":
        problems.append("tags are not immutable")
    if not (found.get("imageScanningConfiguration") or {}).get("scanOnPush"):
        problems.append("scan on push is off")
    if (found.get("encryptionConfiguration") or {}).get("encryptionType") != "AES256":
        problems.append("encryption is not AES256")
    if problems:
        raise RuntimeError("%s does not meet the naming standard: %s" % (name, ", ".join(problems)))
    return found.get("repositoryUri", "")


def ensure(name, template, call=aws):
    """'exists' or 'created'; the repository's settings are read back either way."""
    try:
        call(["describe-repositories", "--repository-names", name, "--output", "json"])
        state = "exists"
    except Missing:
        state = "created"
        call(["create-repository", "--repository-name", name] + SETTINGS)
        try:
            policy = json.loads(call(["get-lifecycle-policy", "--repository-name", template, "--output", "json"]))
        except Missing:
            policy = None
        if policy and policy.get("lifecyclePolicyText"):
            call(["put-lifecycle-policy", "--repository-name", name, "--lifecycle-policy-text", policy["lifecyclePolicyText"]])
    check_settings(name, call)
    return state


def main(argv):
    if len(argv) != 4 or argv[1] != "ensure":
        print("usage: ecr_repository.py ensure <repository> <template repository>", file=sys.stderr)
        return 1
    try:
        print("%s: %s" % (argv[2], ensure(argv[2], argv[3])))
        return 0
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        print("FAIL: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
