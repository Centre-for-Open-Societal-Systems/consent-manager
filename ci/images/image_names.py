"""The ECR names of the Consent Manager images (oan-helm docs/release/naming-standard.md, section 6).

    eoan/consent-manager/<image>:<version>-<branch>.<build>

    eoan/consent-manager/consent-manager-api:1.1.0-develop.57

<version> is in init.yaml at the root of this repository. <build> is the Jenkins build number. The three images
share one tag, so a tag names one build of one source tree. No image carries -<rpVersion>: none is built FROM an
OpenG2P registry platform image (ci/README.md, "No -rpVersion").

    image_names.py tag <branch> <build>   the image tag of a build
    image_names.py repository <image>     the ECR repository of an image, without the registry host
    image_names.py legacy <image>         its Commons repository before eoan (the lifecycle template of a new repository)
    image_names.py list                   image, Dockerfile and build context of every image, tab separated

Standard library only: it runs on the Jenkins agent's stock Python 3.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INIT = ROOT / "init.yaml"
PATH = "eoan/consent-manager"
LEGACY_PATH = "openg2p/commons"
# image name -> (Dockerfile, build context), both relative to the repository root. The same files OpenG2P builds.
IMAGES = {
    "consent-manager-api": ("docker/consent-manager-api/Dockerfile", "."),
    "consent-manager-ui": ("ui/Dockerfile", "ui"),
    "consent-manager-sanity": ("docker/consent-manager-sanity/Dockerfile", "."),
}
FIELDS = ("version",)
VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
LINE = re.compile(r"""^(?P<key>[A-Za-z][A-Za-z0-9_]*)\s*:\s*(?P<value>"[^"]*"|'[^']*'|[^#\s]+)\s*(?:#.*)?$""")
TAG = re.compile(r"\d+\.\d+\.\d+-[a-z0-9]+(?:-[a-z0-9]+)*\.[1-9]\d*")
REPOSITORY = re.compile(re.escape(PATH) + r"/consent-manager-[a-z]+")


def read_init(path=None):
    """init.yaml as {field: value}: flat `key: value` lines and comments, version and nothing else."""
    path = Path(path or INIT)
    fields = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        found = LINE.match(line)
        if not found:
            raise ValueError("%s line %d is not `key: value`" % (path.name, number))
        key = found.group("key")
        if key not in FIELDS or key in fields:
            raise ValueError("%s: %s is unknown or set twice" % (path.name, key))
        fields[key] = found.group("value").strip("\"'")
    if not VERSION.fullmatch(fields.get("version", "")):
        raise ValueError("%s: version is missing or not N.N.N" % path.name)
    return fields


def chart_version(init=None):
    return read_init(init)["version"]


def repository(image):
    if image not in IMAGES:
        raise ValueError("unknown image %r" % image)
    return "%s/%s" % (PATH, image)


def legacy_repository(image):
    if image not in IMAGES:
        raise ValueError("unknown image %r" % image)
    return "%s/%s" % (LEGACY_PATH, image)


def tag(branch, build, init=None):
    """<version>-<branch>.<build>, the form oan-helm's scripts/ci/oan_version.py gives the chart of the same build."""
    slug = re.sub(r"[^a-z0-9]+", "-", (branch or "").lower()).strip("-")
    if slug.isdigit():
        slug = "b" + slug
    if not slug or not re.fullmatch(r"[1-9]\d*", str(build)):
        raise ValueError("an image tag needs a branch and a build number")
    return "%s-%s.%s" % (chart_version(init), slug, build)


def is_tag(value):
    return TAG.fullmatch(value or "") is not None


def is_repository(value):
    return REPOSITORY.fullmatch(value or "") is not None and value in {repository(i) for i in IMAGES}


def main(argv):
    if len(argv) == 4 and argv[1] == "tag":
        print(tag(argv[2], argv[3]))
    elif len(argv) == 3 and argv[1] == "repository":
        print(repository(argv[2]))
    elif len(argv) == 3 and argv[1] == "legacy":
        print(legacy_repository(argv[2]))
    elif argv[1:] == ["list"]:
        for name, (dockerfile, context) in IMAGES.items():
            print("%s\t%s\t%s" % (name, dockerfile, context))
    else:
        raise ValueError("usage: image_names.py tag <branch> <build> | repository <image> | legacy <image> | list")


if __name__ == "__main__":
    try:
        main(sys.argv)
    except (ValueError, OSError) as error:
        sys.exit("FAIL: %s" % error)
