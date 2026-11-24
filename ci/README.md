# Consent Manager build CI

Build-only Jenkins CI for the images of this repository, stored in the OAN ECR (G2R-621). It follows the Farmer
Registry pipeline (`ci/pipelines/dev.groovy`, `ci/deploy/image_names.py`, `ci/build/ecr_repository.py`,
`ci/build/images.sh` of `farmer-registry` develop; here the scripts are in `ci/images/`, because this repository's
`.gitignore` ignores every `build/` directory) and oan-helm's `docs/release/naming-standard.md`, section 6, and
leaves out everything that deploys.

## What a build does

| Stage | Branches and pull requests | `develop` (not a pull request) |
| --- | --- | --- |
| Validate | static and mock tests (`ci/tests`), no network, no AWS, no Docker | same |
| Build | `ci/images/images.sh`: the three images from the unchanged Dockerfiles, started offline | same |
| Publish | not run, no ECR call | `ci/images/publish_images.py` |

No deploy, no Helm change, no push to oan-helm. The manifest `service-outcomes/consent-manager-images.json` (and its
`.tsv`) is archived instead. Hand it to registry-test, then to a separate oan-helm promotion that pins the digests.

## Images and names

```text
379220350808.dkr.ecr.ap-south-1.amazonaws.com/eoan/consent-manager/<image>:<version>-develop.<BUILD_NUMBER>
```

| Image | Dockerfile (as OpenG2P builds it) | Context |
| --- | --- | --- |
| `consent-manager-api` | `docker/consent-manager-api/Dockerfile` | `.` |
| `consent-manager-ui` | `ui/Dockerfile` | `ui` |
| `consent-manager-sanity` | `docker/consent-manager-sanity/Dockerfile` | `.` |

All three share one tag, for example `1.1.0-develop.57`: `<version>` from `init.yaml`, `develop`, the Jenkins build
number. `publish_images.py` recomputes the tag from `init.yaml`, `BRANCH_NAME` and `BUILD_NUMBER` and refuses any other
`IMAGE_TAG`; it also requires a clean work tree at `SOURCE_COMMIT`. The images carry
`org.opencontainers.image.source` (this repository) and `org.opencontainers.image.revision` (the 40 character commit),
set by `images.sh` from the commit alone (the UI's `BUILD_TIME` is the commit date), so the same commit gives the same
labels. The manifest records the exact `<registry>/<repository>:<tag>@sha256:<digest>`, the source and revision.

### The version in `init.yaml` is 1.1.0

Evidence (read 24 November 2026):

- OpenG2P tags `1.0.0` to `1.0.4`; `1.0.4` is `41f69db`, the base of this repository's `develop` (`bb2efdf`).
- `develop` adds the OTP consent changes on top: 35 files, 4131 insertions, a new feature and not a fix.
- The Python package still reports `__version__ = "1.0.3"` (the OpenAPI version), and the chart's images are `1.0.3`.
  Those files are native OpenG2P files and are not changed here.
- The Commons install of 5 November 2026 runs `dev-48e64c249056` images, chart `0.0.0-develop.91`: no release line.

An OAN image with new behaviour must not be taken for the public `openg2p/consent-manager-*:1.0.x` images, so the line
is a minor above them: `1.1.0`. Bump `init.yaml` for the next release; never reuse a `<version>-develop.<n>` (below).

### No `-rpVersion` suffix

The standard puts `-<rpVersion>` only on images built FROM an OpenG2P registry platform image
(`FROM openg2p/openg2p-registry-<role>:${BASE_IMAGE_VERSION}`), because a platform bump creates new repositories for
them. These images are built FROM `python:3.12-slim-bookworm`, `node:20-alpine` and `nginxinc/nginx-unprivileged`; the
API installs the OpenG2P framework as a library (`ARG FASTAPI_COMMON_REF=1.3.0`) and no Dockerfile has a
`BASE_IMAGE_VERSION`. A framework bump is a source change that bumps `init.yaml`, not a new repository.

### The earlier Commons convention, and why it is not kept

Commons Services ran `.../openg2p/commons/consent-manager-api:dev-<commit12>` (tag from the commit, repository under
`openg2p/commons`, chart `0.0.0-develop.<n>`). Kept: the image names, and that repository's lifecycle policy, which a
new repository copies when it has one. Not kept: a tag that does not say which release or build it is, a repository
outside `eoan/<repository>/`, and a commit tag that is not a build identity (two builds of one commit would collide or
be replaced). The standard's `<version>-develop.<n>` names the build, and the digest is what runs.

## ECR repositories and safety

`ensure` creates a missing repository with immutable tags, scan on push, AES256, and the tags `owner=consent-manager`
and `naming=eoan-standard`, then reads the settings back; an existing repository that is mutable, unscanned or not
AES256 fails the build (Farmer only leaves it alone). Publishing fails closed, before the first push, when:

- the branch is not `develop`, or `CHANGE_ID` or `CHANGE_TARGET` is set;
- `AWS_REGION` is not `ap-south-1`, or the credential's account is not `379220350808` (the registry of every image in oan-helm);
- an `aws` call fails for any reason other than "not found": credentials, access, throttling or network never create anything;
- the tag already exists in any of the three repositories (no partial set is pushed; bump `init.yaml`);
- a local image lacks the OCI source and revision of this commit.

After each push the tag is read back from the registry: it must hold the digest `docker push` reported, in this
account. The AWS credential comes only from the Jenkins binding (`aws-ecr-creds`, `AmazonWebServicesCredentialsBinding`)
around the Publish step; Docker receives the login password on stdin, in a temporary `DOCKER_CONFIG`, and error text is
masked. There is no pipeline `retry`: a repeated run of the same build number would find its own tags published.

## Inherited OpenG2P publishing is off here

`.github/workflows/build-publish.yml` (OpenG2P's reusable Docker Hub and chart publisher) now has
`if: github.repository == 'openg2p/consent-manager'`, so it is skipped in this repository and Jenkins is the only
publisher. The rest of the file is unchanged. Upstream's own repository is unaffected.

## Jenkins wiring required (nothing was created)

1. A multibranch pipeline job for `Centre-for-Open-Societal-Systems/consent-manager`, script path `Jenkinsfile`, branch
   discovery and pull request discovery from origin, with a GitHub credential that can read the repository.
2. The credential `aws-ecr-creds` (AWS access key and secret, as for Farmer) with ECR rights on `eoan/consent-manager/*`
   in account `379220350808`: `ecr:GetAuthorizationToken`, `DescribeRepositories`, `CreateRepository`, `TagResource`,
   `GetLifecyclePolicy` and `PutLifecyclePolicy` on `openg2p/commons/*`, `PutLifecyclePolicy`, `DescribeImages`,
   `BatchCheckLayerAvailability`, `InitiateLayerUpload`, `UploadLayerPart`, `CompleteLayerUpload`, `PutImage`,
   `BatchGetImage`, and `sts:GetCallerIdentity`. The identity policy must cover new repositories under the prefix
   (`docs/ecr-pull-access.md` in oan-helm).
3. An agent with Docker (and the AWS CLI and `python3`, `git`, `bash`) and Internet access to Docker Hub, npm, PyPI and
   GitHub for the build. Pull requests need only the Docker and validation parts: they never use the credential.
4. The GitHub webhook or the periodic branch scan, so `develop` builds on merge.

## Tests

```text
python3 -m unittest discover -s ci/tests -v
```

Standard library only. `aws`, `docker` and `git` are replaced by a recording stand-in (`Cloud`), and `images.sh` runs
against a stub `docker`, so publication (create, exists, refused settings, tag collision, auth and API errors, wrong
region or account, digest mismatch, no secret in output) and the pipeline wiring are tested without AWS or an engine.
The build itself (real Docker builds, the offline start checks of the images and the label override on the UI image)
has to run once on the Jenkins agent.
