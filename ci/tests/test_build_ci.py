"""Tests of the build-only CI: naming, ECR repository handling, publication with a recording stand-in for aws and docker,
the Jenkins wiring and the dependency pins. No network, no AWS, no Docker. Standard library only (python3 -m unittest)."""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "ci" / "images"
sys.path.insert(0, str(BUILD))
import ecr_repository as ecr  # noqa: E402
import image_names  # noqa: E402
import publish_images as pub  # noqa: E402

COMMIT = "0123456789abcdef0123456789abcdef01234567"
SOURCE = pub.SOURCE
ACCOUNT = pub.EXPECTED_ACCOUNT
TAG = "1.1.0-develop.7"
REPOS = ["eoan/consent-manager/consent-manager-api", "eoan/consent-manager/consent-manager-ui",
         "eoan/consent-manager/consent-manager-sanity"]
GOOD = {"imageTagMutability": "IMMUTABLE", "imageScanningConfiguration": {"scanOnPush": True},
        "encryptionConfiguration": {"encryptionType": "AES256"}}
SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEYEXAMPLEKEY"


class Cloud:
    """A recording stand-in for aws, docker and git. `faults` maps a command prefix to (returncode, stderr)."""

    def __init__(self, account=ACCOUNT, existing=(), taken=(), settings=None, faults=None, labels=None, head=COMMIT,
                 dirty="", digests=None, lookup_digest=None):
        self.account, self.repos, self.taken = account, set(existing), set(taken)
        self.settings = settings or {}
        self.faults, self.calls, self.stdin = faults or {}, [], []
        self.labels = labels if labels is not None else {"org.opencontainers.image.source": SOURCE, "org.opencontainers.image.revision": COMMIT}
        self.head, self.dirty = head, dirty
        self.digests, self.lookup_digest = digests or {}, lookup_digest
        self.pushed = {}

    def digest(self, repository):
        return self.digests.get(repository, "sha256:" + format(REPOS.index(repository) + 1, "x") * 64)

    def __call__(self, argv, stdin=None):
        self.calls.append(list(argv))
        if stdin is not None:
            self.stdin.append(stdin)
        text = " ".join(argv)
        for prefix, (code, err) in self.faults.items():
            if text.startswith(prefix):
                return code, "", err
        if argv[:2] == ["git", "rev-parse"]:
            return 0, self.head + "\n", ""
        if argv[:2] == ["git", "status"]:
            return 0, self.dirty, ""
        if argv[:3] == ["docker", "image", "inspect"]:
            return 0, json.dumps(self.labels), ""
        if argv[:2] == ["docker", "login"] or argv[:2] == ["docker", "tag"]:
            return 0, "", ""
        if argv[:2] == ["docker", "push"]:
            repository = argv[2].split("/", 1)[1].rsplit(":", 1)[0]
            self.pushed[repository] = argv[2]
            return 0, "latest: digest: %s size: 1\n" % self.digest(repository), ""
        assert argv[0] == "aws", argv
        if argv[1] == "sts":
            return 0, self.account + "\n", ""
        if argv[1:3] == ["ecr", "get-login-password"]:
            return 0, SECRET + "\n", ""
        verb = argv[4]
        name = argv[argv.index("--repository-name" if "--repository-name" in argv else "--repository-names") + 1]
        if verb == "describe-repositories":
            if name not in self.repos:
                return 255, "", "An error occurred (RepositoryNotFoundException)"
            return 0, json.dumps({"repositories": [dict(GOOD, repositoryName=name, **self.settings.get(name, {}))]}), ""
        if verb == "create-repository":
            self.repos.add(name)
            return 0, "{}", ""
        if verb == "get-lifecycle-policy":
            return 255, "", "An error occurred (LifecyclePolicyNotFoundException)"
        if verb == "describe-images":
            tag = argv[argv.index("--image-ids") + 1].split("=", 1)[1]
            if (name, tag) in self.taken:
                digest = self.digest(name)
            elif name in self.pushed:
                digest = self.lookup_digest or self.digest(name)
            else:
                return 255, "", "An error occurred (ImageNotFoundException)"
            return 0, json.dumps({"imageDetails": [{"imageDigest": digest, "imageTags": [tag], "registryId": ACCOUNT}]}), ""
        raise AssertionError(argv)

    def verbs(self):
        return [c[4] for c in self.calls if c[:2] == ["aws", "ecr"] and len(c) > 4]

    def ran(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


def environment(**changes):
    env = {"BRANCH_NAME": "develop", "BUILD_NUMBER": "7", "IMAGE_TAG": TAG, "SOURCE_COMMIT": COMMIT, "AWS_REGION": "ap-south-1",
           "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE", "AWS_SECRET_ACCESS_KEY": SECRET}
    env.update(changes)
    return {k: v for k, v in env.items() if v is not None}


class Names(unittest.TestCase):
    def test_the_standard_repository_and_tag(self):
        self.assertEqual(image_names.repository("consent-manager-api"), REPOS[0])
        self.assertEqual(image_names.tag("develop", 57), "1.1.0-develop.57")
        self.assertEqual(image_names.tag("PR-12", 3), "1.1.0-pr-12.3")

    def test_no_image_carries_a_platform_version(self):
        for name in image_names.IMAGES:
            self.assertRegex(image_names.repository(name), r"^eoan/consent-manager/consent-manager-(api|ui|sanity)$")

    def test_one_tag_for_every_image_and_unknown_images_are_refused(self):
        self.assertEqual({image_names.tag("develop", 9)}, {image_names.tag("develop", 9) for _ in image_names.IMAGES})
        with self.assertRaises(ValueError):
            image_names.repository("consent-manager-other")
        with self.assertRaises(ValueError):
            image_names.tag("develop", "0")

    def test_init_yaml_accepts_only_a_version(self):
        with tempfile.TemporaryDirectory() as folder:
            for text in ("version: 1.1\n", "version: 1.1.0\nversion: 1.1.1\n", "version: 1.1.0\nrpVersion: 1\n", "nonsense\n"):
                path = Path(folder) / "init.yaml"
                path.write_text(text)
                with self.assertRaises(ValueError):
                    image_names.read_init(path)
            path.write_text("# comment\nversion: 2.0.1  # note\n")
            self.assertEqual(image_names.read_init(path), {"version": "2.0.1"})

    def test_the_dockerfiles_listed_exist(self):
        for dockerfile, context in image_names.IMAGES.values():
            self.assertTrue((ROOT / dockerfile).is_file())
            self.assertTrue((ROOT / context).is_dir())


class Repository(unittest.TestCase):
    def test_a_missing_repository_is_created_with_the_standard_settings(self):
        cloud = Cloud()
        call = lambda args: ecr.aws(args, cloud)  # noqa: E731
        self.assertEqual(ecr.ensure(REPOS[0], "openg2p/commons/consent-manager-api", call), "created")
        create = cloud.ran("aws", "ecr", "--region", "ap-south-1", "create-repository")[0]
        self.assertEqual(create[create.index("--image-tag-mutability") + 1], "IMMUTABLE")
        self.assertIn("scanOnPush=true", create)
        self.assertIn("encryptionType=AES256", create)
        self.assertIn("Key=owner,Value=consent-manager", create)
        self.assertIn("Key=naming,Value=eoan-standard", create)
        self.assertTrue(all(c[3] == "ap-south-1" for c in cloud.calls))

    def test_an_existing_standard_repository_is_only_read(self):
        cloud = Cloud(existing=[REPOS[0]])
        self.assertEqual(ecr.ensure(REPOS[0], "x", lambda a: ecr.aws(a, cloud)), "exists")
        self.assertNotIn("create-repository", cloud.verbs())

    def test_an_existing_repository_off_the_standard_is_refused(self):
        for bad, text in (({"imageTagMutability": "MUTABLE"}, "immutable"),
                          ({"imageScanningConfiguration": {"scanOnPush": False}}, "scan"),
                          ({"encryptionConfiguration": {"encryptionType": "KMS"}}, "AES256")):
            cloud = Cloud(existing=[REPOS[0]], settings={REPOS[0]: bad})
            with self.assertRaisesRegex(RuntimeError, text):
                ecr.ensure(REPOS[0], "x", lambda a: ecr.aws(a, cloud))

    def test_an_api_error_is_never_taken_for_a_missing_repository(self):
        for error in ("AccessDeniedException", "ExpiredTokenException", "Could not connect to the endpoint URL"):
            cloud = Cloud(faults={"aws ecr --region ap-south-1 describe-repositories": (254, "An error occurred (%s)" % error)})
            with self.assertRaises(RuntimeError):
                ecr.ensure(REPOS[0], "x", lambda a: ecr.aws(a, cloud))
            self.assertNotIn("create-repository", cloud.verbs())


class Publication(unittest.TestCase):
    def publish(self, cloud, **env):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        path = Path(folder) / "out" / "consent-manager-images.json"
        return pub.publish(environment(**env), cloud, path), path

    def refused(self, cloud, pattern, **env):
        with self.assertRaisesRegex((pub.Refused, RuntimeError), pattern):
            self.publish(cloud, **env)
        return cloud

    def test_a_develop_build_publishes_three_images_and_the_manifest(self):
        cloud = Cloud()
        result, path = self.publish(cloud)
        self.assertEqual(cloud.verbs().count("create-repository"), 3)
        self.assertEqual(len(cloud.ran("docker", "push")), 3)
        registry = "%s.dkr.ecr.ap-south-1.amazonaws.com" % ACCOUNT
        for name, repository in zip(image_names.IMAGES, REPOS):
            image = result["images"][name]
            self.assertEqual(image["reference"], "%s/%s:%s@%s" % (registry, repository, TAG, cloud.digest(repository)))
            self.assertEqual((image["ociSource"], image["ociRevision"]), (SOURCE, COMMIT))
        written = json.loads(path.read_text())
        self.assertEqual(written["sourceCommit"], COMMIT)
        self.assertEqual((written["deployment"], written["helm"]), ("not-attempted", "not-changed"))
        self.assertEqual(len(path.with_suffix(".tsv").read_text().splitlines()), 3)

    def test_the_same_immutable_tag_is_pushed_for_every_image(self):
        cloud = Cloud(existing=REPOS)
        self.publish(cloud)
        pushed = [c[2].rsplit(":", 1)[1] for c in cloud.ran("docker", "push")]
        self.assertEqual(pushed, [TAG] * 3)
        self.assertEqual(cloud.verbs().count("create-repository"), 0)

    def test_the_credential_goes_to_docker_on_stdin_only(self):
        cloud = Cloud()
        self.publish(cloud)
        login = cloud.ran("docker", "login")[0]
        self.assertIn("--password-stdin", login)
        self.assertEqual(cloud.stdin, [SECRET + "\n"])
        self.assertNotIn(SECRET, " ".join(" ".join(c) for c in cloud.calls))

    def test_only_develop_publishes_and_a_refusal_touches_nothing(self):
        for env in ({"BRANCH_NAME": "feature-x", "IMAGE_TAG": "1.1.0-feature-x.7"}, {"BRANCH_NAME": "PR-4", "CHANGE_ID": "4", "IMAGE_TAG": "1.1.0-pr-4.7"},
                    {"CHANGE_ID": "4"}, {"CHANGE_TARGET": "develop"}, {"BRANCH_NAME": "staging", "IMAGE_TAG": "1.1.0-staging.7"}):
            cloud = Cloud()
            self.refused(cloud, "only a develop branch", **env)
            self.assertEqual(cloud.calls, [])

    def test_wrong_region_account_tag_and_source_are_refused_before_ecr(self):
        self.refused(Cloud(), "AWS_REGION", AWS_REGION="us-east-1")
        self.refused(Cloud(), "AWS_REGION", AWS_REGION=None)
        self.refused(Cloud(), "IMAGE_TAG", IMAGE_TAG="1.1.0-develop.8")
        self.refused(Cloud(), "IMAGE_TAG", IMAGE_TAG="latest")
        self.refused(Cloud(), "SOURCE_COMMIT", SOURCE_COMMIT="abc123")
        self.refused(Cloud(head="f" * 40), "not at SOURCE_COMMIT")
        self.refused(Cloud(dirty=" M backend/pyproject.toml\n"), "work tree has changes")
        cloud = self.refused(Cloud(account="111111111111"), "not the OAN ECR account")
        self.assertEqual(cloud.verbs(), [])
        self.refused(Cloud(account="None"), "not an AWS account id")

    def test_images_that_are_not_this_commit_are_refused(self):
        stale = {"org.opencontainers.image.source": SOURCE, "org.opencontainers.image.revision": "e" * 40}
        foreign = {"org.opencontainers.image.source": "https://github.com/openg2p/consent-manager", "org.opencontainers.image.revision": COMMIT}
        for labels in (stale, foreign, {}):
            cloud = self.refused(Cloud(labels=labels), "OCI source and revision")
            self.assertEqual(cloud.ran("docker", "push"), [])

    def test_a_tag_collision_fails_before_any_push(self):
        for taken in (0, 1, 2):
            cloud = Cloud(existing=REPOS, taken=[(REPOS[taken], TAG)])
            self.refused(cloud, "tag collision")
            self.assertEqual(cloud.ran("docker", "push"), [])
            self.assertEqual(cloud.ran("docker", "login"), [])

    def test_auth_and_api_errors_fail_closed(self):
        for prefix, error in (("aws sts", "ExpiredToken"), ("aws ecr --region ap-south-1 describe-repositories", "AccessDeniedException"),
                              ("aws ecr --region ap-south-1 describe-images", "ThrottlingException"),
                              ("aws ecr --region ap-south-1 create-repository", "RepositoryPolicyException"),
                              ("aws ecr get-login-password", "UnrecognizedClientException"),
                              ("docker login", "unauthorized"), ("docker push", "denied: requested access"),
                              ("docker tag", "no such image")):
            cloud = Cloud(existing=REPOS if "create" not in prefix else (), faults={prefix: (254, "An error occurred (%s)" % error)})
            self.refused(cloud, "failed|ExpiredToken|AccessDenied|Throttling|RepositoryPolicy")
            self.assertEqual(cloud.ran("docker", "push") if "docker push" not in prefix else [], [])

    def test_a_registry_digest_that_differs_from_the_push_fails(self):
        cloud = Cloud(existing=REPOS, lookup_digest="sha256:" + "9" * 64)
        self.refused(cloud, "does not hold")

    def test_a_push_without_a_digest_fails(self):
        cloud = Cloud(existing=REPOS)
        original = cloud.__call__
        cloud.__class__ = type("Silent", (Cloud,), {"__call__": lambda self, a, s=None: (0, "pushed\n", "") if a[:2] == ["docker", "push"] else original(a, s)})
        self.refused(cloud, "no digest")

    def test_errors_never_print_a_secret(self):
        cloud = Cloud(existing=REPOS, faults={"docker login": (1, "Error response: bad token %s" % SECRET)})
        with self.assertRaises(pub.Refused) as caught:
            self.publish(cloud)
        self.assertNotIn(SECRET, str(caught.exception))

    def test_the_command_line_exits_one_and_prints_no_secret(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        cloud = Cloud(existing=REPOS, taken=[(REPOS[0], TAG)])
        from io import StringIO
        out, err = StringIO(), StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = pub.main(["publish_images.py", str(Path(folder) / "m.json")], environment(), cloud)
        finally:
            sys.stdout, sys.stderr = old
        self.assertEqual(code, 1)
        self.assertIn("tag collision", err.getvalue())
        self.assertNotIn(SECRET, out.getvalue() + err.getvalue())
        self.assertFalse((Path(folder) / "m.json").exists())


class Wiring(unittest.TestCase):
    def text(self, *parts):
        return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")

    def test_publishing_is_develop_only_and_the_credential_is_a_binding(self):
        groovy = self.text("ci", "pipelines", "dev.groovy")
        publish = groovy[groovy.index("stage('Publish')"):groovy.index("post {")]
        self.assertIn("branch 'develop'", publish)
        self.assertIn("not { changeRequest() }", publish)
        self.assertIn("AmazonWebServicesCredentialsBinding", publish)
        self.assertEqual(groovy.count("AmazonWebServicesCredentialsBinding"), 1)
        self.assertEqual(groovy.count("aws-ecr-creds"), 1)
        self.assertIn("set +x", publish)
        for stage in ("Validate", "Build"):
            part = groovy[groovy.index("stage('%s')" % stage):]
            part = part[:part.index("stage(", 10)] if "stage(" in part[10:] else part
            self.assertNotIn("credentials", part.lower())
            self.assertNotIn("aws ", part)
        self.assertNotRegex(groovy, r"(?i)helm|kubectl")
        self.assertNotRegex(groovy, r"git push|git commit|deploy\(|AWS_SECRET|AWS_ACCESS_KEY")
        self.assertNotIn("retry(", groovy)

    def test_no_secret_or_account_key_is_in_the_ci_files(self):
        for path in list((ROOT / "ci").rglob("*")) + [ROOT / "Jenkinsfile"]:
            if path.is_file() and path.suffix in {".py", ".sh", ".groovy", ""} and "tests" not in path.parts:
                content = path.read_text(encoding="utf-8")
                self.assertNotRegex(content, r"AKIA[0-9A-Z]{12,}|aws_secret_access_key\s*=|BEGIN [A-Z ]*PRIVATE KEY", str(path))

    def test_the_inherited_openg2p_publisher_is_off_here(self):
        workflow = self.text(".github", "workflows", "build-publish.yml")
        self.assertIn("if: github.repository == 'openg2p/consent-manager'", workflow)
        self.assertIn("openg2p/openg2p-packaging/.github/workflows/build-publish.yml@v1", workflow)

    def test_the_upstream_pins_and_native_files_are_the_ones_openg2p_builds(self):
        workflow = self.text(".github", "workflows", "build-publish.yml")
        api = self.text("docker", "consent-manager-api", "Dockerfile")
        default = re.search(r"(?m)^ARG FASTAPI_COMMON_REF=(\S+)$", api).group(1)
        self.assertEqual(default, "1.3.0")
        self.assertIn('"ref": "%s"' % default, workflow)
        self.assertEqual(self.text("backend", "src", "openg2p_consent_manager", "__init__.py").strip(), '__version__ = "1.0.3"')
        self.assertTrue((ROOT / "ui" / "package-lock.json").is_file())
        self.assertIn("npm ci", self.text("ui", "Dockerfile"))
        for path in ROOT.glob("docker/*/Dockerfile"):
            self.assertNotIn("BASE_IMAGE_VERSION", path.read_text(encoding="utf-8"))
        for dockerfile, _ in image_names.IMAGES.values():
            self.assertNotRegex(self.text(*dockerfile.split("/")), r"openg2p/openg2p-registry")

    def test_the_build_script_overrides_no_pin(self):
        script = self.text("ci", "images", "images.sh")
        self.assertNotIn("FASTAPI_COMMON_REF", script)
        self.assertNotIn("--no-cache", script)
        build_args = re.findall(r"--build-arg \"?([A-Z_]+)", script)
        self.assertEqual(sorted(build_args), ["BUILD_TIME", "GIT_COMMIT"])


def find_bash():
    """A real bash: not Windows' WSL launcher, which has no distribution here. Git for Windows ships one."""
    candidates = [os.environ.get("BASH_EXE"), shutil.which("bash")]
    git = shutil.which("git")
    if git:
        candidates += [str(Path(git).parents[1] / "bin" / "bash.exe"), str(Path(git).parents[1] / "usr" / "bin" / "bash.exe")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and "system32" not in candidate.lower().replace("\\", "/"):
            return candidate
    return None


BASH = find_bash()


@unittest.skipUnless(BASH, "needs bash")
class BuildScript(unittest.TestCase):
    """ci/images/images.sh against a stand-in docker that records its commands."""

    def run_script(self, commit=None, revision=None):
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout.strip()
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        log = folder / "docker.log"
        stub = folder / "docker"
        stub.write_text("#!/bin/sh\necho \"$@\" >> \"$STUB_LOG\"\ncase \"$*\" in\n"
                        "*revision*) echo \"$STUB_REVISION\";;\n*source*) echo \"$STUB_SOURCE\";;\nesac\n", newline="\n")
        shim = folder / "python3"
        shim.write_text("#!/bin/sh\nexec \"%s\" \"$@\"\n" % Path(sys.executable).as_posix(), newline="\n")
        for item in (stub, shim):
            item.chmod(item.stat().st_mode | stat.S_IEXEC)
        env = dict(os.environ, PATH=folder.as_posix() + os.pathsep + os.environ["PATH"], STUB_LOG=log.as_posix(),
                   STUB_REVISION=revision or head, STUB_SOURCE=SOURCE)
        done = subprocess.run([BASH, "ci/images/images.sh", TAG, commit or head], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True)
        return done, (log.read_text().splitlines() if log.exists() else []), head

    def test_it_builds_the_three_images_from_the_unchanged_dockerfiles(self):
        done, log, head = self.run_script()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        builds = [line for line in log if line.startswith("build ")]
        self.assertEqual(len(builds), 3)
        self.assertIn("-f docker/consent-manager-api/Dockerfile", builds[0])
        self.assertTrue(builds[0].endswith("-t oan-ci/consent-manager-api:%s ." % TAG))
        self.assertIn("-f ui/Dockerfile", builds[1])
        self.assertIn("GIT_COMMIT=%s" % head, builds[1])
        self.assertTrue(builds[1].endswith("-t oan-ci/consent-manager-ui:%s ui" % TAG))
        self.assertIn("-f docker/consent-manager-sanity/Dockerfile", builds[2])
        for line in builds:
            self.assertIn("org.opencontainers.image.revision=%s" % head, line)
            self.assertIn("org.opencontainers.image.source=%s" % SOURCE, line)
            self.assertNotIn("FASTAPI_COMMON_REF", line)
        self.assertEqual(len([line for line in log if line.startswith("run ") and "--network none" in line]), 3)

    def test_a_commit_that_is_not_the_checkout_is_refused_before_a_build(self):
        done, log, _ = self.run_script(commit="a" * 40)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(log, [])

    def test_an_image_with_another_revision_label_fails_the_build(self):
        done, _, _ = self.run_script(revision="b" * 40)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("carries revision", done.stderr)


if __name__ == "__main__":
    unittest.main()
