#!/usr/bin/env sh
# Compile Yellowstone / Geyser gRPC stubs when protos/geyser.proto is present.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROTO_DIR="${ROOT}/protos"
OUT_DIR="${ROOT}/src/core/generated"
PROTO_FILE="${PROTO_DIR}/geyser.proto"

if [ ! -f "${PROTO_FILE}" ]; then
  echo "compile_geyser_protos: skip (no ${PROTO_FILE})"
  exit 0
fi

mkdir -p "${OUT_DIR}"
touch "${OUT_DIR}/__init__.py"

python -m grpc_tools.protoc \
  -I"${PROTO_DIR}" \
  --python_out="${OUT_DIR}" \
  --grpc_python_out="${OUT_DIR}" \
  "${PROTO_FILE}"

echo "compile_geyser_protos: wrote stubs to ${OUT_DIR}"
