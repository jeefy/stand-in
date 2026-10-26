#!/usr/bin/env bash
# Regenerate the checked-in Envoy ext_proc gRPC stubs under
# src/standin/_proto/ from the upstream API protos.
#
# Pins (the xds and protoc-gen-validate revisions are the ones Envoy's own
# api/bazel/repository_locations.bzl uses at ENVOY_API_VERSION):
#   envoyproxy/envoy              api/ at  v1.39.4
#   cncf/xds (xds/, udpa/)        at       8bfbf64dc13ee1a570be4fbdcfccbdd8532463f0
#   bufbuild/protoc-gen-validate  at       v1.3.3
#   google/protobuf/*             bundled with grpcio-tools (not fetched)
#   grpcio-tools                  1.81.1   (protoc gencode must not be newer
#                                          than the locked protobuf runtime, <7)
#
# Nothing is trimmed: the script starts at
# envoy/service/ext_proc/v3/external_processor.proto and fetches its full
# transitive import closure file by file (no tarballs), so the generated tree
# is exactly what that one service needs.
#
# Generated modules import each other by their proto package path
# (`from envoy.config.core.v3 import base_pb2`); those imports are rewritten to
# `standin._proto.<pkg>` so no top-level `envoy`/`validate`/`xds`
# packages are installed into the environment. Proto file names inside the
# descriptors are unchanged, so the wire format is unaffected.
#
# Usage: scripts/gen-proto.sh   (needs curl + uv)
set -euo pipefail

ENVOY_API_VERSION=v1.39.4
XDS_COMMIT=8bfbf64dc13ee1a570be4fbdcfccbdd8532463f0
PGV_VERSION=v1.3.3
GRPCIO_TOOLS_VERSION=1.81.1
ROOT_PROTO=envoy/service/ext_proc/v3/external_processor.proto

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="${here}/src/standin/_proto"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

src="${work}/src"
gen="${work}/gen"
mkdir -p "${src}" "${gen}"

url_for() {
    local path="$1"
    case "${path}" in
        envoy/*) echo "https://raw.githubusercontent.com/envoyproxy/envoy/${ENVOY_API_VERSION}/api/${path}" ;;
        xds/* | udpa/*) echo "https://raw.githubusercontent.com/cncf/xds/${XDS_COMMIT}/${path}" ;;
        validate/*) echo "https://raw.githubusercontent.com/bufbuild/protoc-gen-validate/${PGV_VERSION}/${path}" ;;
        *) return 1 ;;
    esac
}

declare -A seen=()
queue=("${ROOT_PROTO}")
files=()
while ((${#queue[@]})); do
    path="${queue[0]}"
    queue=("${queue[@]:1}")
    [[ -n "${seen[${path}]:-}" ]] && continue
    seen["${path}"]=1
    # Well-known types ship with grpcio-tools.
    [[ "${path}" == google/protobuf/* ]] && continue
    if ! url="$(url_for "${path}")"; then
        echo "gen-proto: no source mapping for import ${path}" >&2
        exit 1
    fi
    mkdir -p "${src}/$(dirname "${path}")"
    curl -fsSL --retry 3 --connect-timeout 15 "${url}" -o "${src}/${path}"
    files+=("${path}")
    while IFS= read -r dep; do
        queue+=("${dep}")
    done < <(sed -nE 's/^import[[:space:]]+(public[[:space:]]+|weak[[:space:]]+)?"([^"]+)";.*/\2/p' "${src}/${path}")
done
echo "gen-proto: ${#files[@]} proto files in the closure of ${ROOT_PROTO}"

(
    cd "${src}"
    uv run --no-project --quiet --with "grpcio-tools==${GRPCIO_TOOLS_VERSION}" \
        python -m grpc_tools.protoc -I. \
        --python_out="${gen}" --pyi_out="${gen}" --grpc_python_out="${gen}" \
        "${files[@]}"
)

# Only the ext_proc service has RPCs; drop the empty *_pb2_grpc.py stubs.
find "${gen}" -name '*_pb2_grpc.py' ! -path '*/ext_proc/v3/external_processor_pb2_grpc.py' -delete

# Re-root generated imports under standin._proto.
find "${gen}" \( -name '*.py' -o -name '*.pyi' \) -print0 |
    xargs -0 sed -i -E \
        -e 's/^from (envoy|xds|udpa|validate)([. ])/from standin._proto.\1\2/' \
        -e 's/^import (envoy|xds|udpa|validate)\./import standin._proto.\1./'

# Regular packages all the way down (hatchling + type checkers).
find "${gen}" -type d -exec touch {}/__init__.py \;

rm -rf "${out}"
mkdir -p "${out}"
cp -r "${gen}/." "${out}/"
cat > "${out}/__init__.py" <<EOF
"""Generated Envoy ext_proc stubs. Do not edit; run scripts/gen-proto.sh.

envoy api ${ENVOY_API_VERSION}, cncf/xds ${XDS_COMMIT},
protoc-gen-validate ${PGV_VERSION}, grpcio-tools ${GRPCIO_TOOLS_VERSION}.
"""
EOF
echo "gen-proto: wrote $(find "${out}" -name '*.py' | wc -l) modules to ${out#"${here}/"}"
