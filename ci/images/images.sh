#!/usr/bin/env bash
# Build the Consent Manager images from the repository's own Dockerfiles, unchanged, and check that each one starts.
#
#   bash ci/images/images.sh <image tag> <source commit>
#
# The Dockerfiles are OpenG2P's (docker/consent-manager-api, docker/consent-manager-sanity, ui): no build argument
# overrides a dependency pin, so the openg2p-fastapi-common ref and the npm lock file are the ones in the commit. The only
# additions are labels and the UI's two documented build arguments, all derived from the commit: the same commit
# gives the same labels. Images are tagged oan-ci/<image>:<tag>; ci/images/publish_images.py pushes them.
set -euo pipefail
tag="${1:?usage: ci/images/images.sh <image tag> <source commit>}"
commit="${2:?usage: ci/images/images.sh <image tag> <source commit>}"
case "$commit" in *[!0-9a-f]*|'') echo "FAIL: the source commit is not a hex commit" >&2; exit 1 ;; esac
test "${#commit}" = 40 || { echo "FAIL: the source commit is not 40 characters" >&2; exit 1; }
test "$(git rev-parse --verify HEAD)" = "$commit" || { echo "FAIL: the checkout is not at $commit" >&2; exit 1; }
python3 ci/images/image_names.py tag develop 1 >/dev/null  # init.yaml must be valid before anything is built

source_url=https://github.com/Centre-for-Open-Societal-Systems/consent-manager
committed=$(git show -s --format=%cI "$commit")

while IFS=$'\t' read -r image dockerfile context; do
  extra=()
  if [ "$image" = consent-manager-ui ]; then
    # The UI Dockerfile writes these two build arguments into its own revision and created labels.
    extra=(--build-arg "GIT_COMMIT=$commit" --build-arg "BUILD_TIME=$committed")
  fi
  docker build -f "$dockerfile" \
    --label "org.opencontainers.image.source=$source_url" \
    --label "org.opencontainers.image.revision=$commit" \
    --label "org.opencontainers.image.version=$tag" \
    ${extra[@]+"${extra[@]}"} -t "oan-ci/$image:$tag" "$context"
done < <(python3 ci/images/image_names.py list)

# Build health, offline: the API imports its package and the framework it is built on, the sanity image collects its
# tests, and the UI holds its built page.
docker run --rm --network none --entrypoint python "oan-ci/consent-manager-api:$tag" \
  -c "import openg2p_consent_manager, openg2p_fastapi_common"
docker run --rm --network none --entrypoint python "oan-ci/consent-manager-sanity:$tag" \
  -m pytest --collect-only -q -p no:cacheprovider
docker run --rm --network none --entrypoint sh "oan-ci/consent-manager-ui:$tag" \
  -c 'test -s /usr/share/nginx/html/index.html'

# The labels the images actually carry, as the registry-bound images will.
for image in consent-manager-api consent-manager-ui consent-manager-sanity; do
  revision=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "oan-ci/$image:$tag")
  test "$revision" = "$commit" || { echo "FAIL: $image carries revision $revision, not $commit" >&2; exit 1; }
  source=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.source"}}' "oan-ci/$image:$tag")
  test "$source" = "$source_url" || { echo "FAIL: $image carries source $source" >&2; exit 1; }
done
