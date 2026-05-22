#!/usr/bin/env bash
# Lint via `pio run` using xtensa-gcc (the only compiler that fully understands
# the ESP32-S3 SDK on this toolchain). Aggressive warnings are configured in
# platformio.ini's `build_src_flags`, so they apply to src/ + include/ only and
# not to library code we cannot fix.
#
# Mainstream clang-tidy was tried but cannot parse the xtensa-S3 SDK on macOS
# host (inline-asm constraints, mach-o vs xtensa section attributes, pointer
# truncation in 64-bit host). Stick with what works.
#
# Usage: scripts/lint.sh

set -euo pipefail
cd "$(dirname "$0")/.."

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

pio run 2>&1 | tee "$TMP" >/dev/null

HITS=$(grep -E "^(${PWD}/)?(src|include)/[^:]+:[0-9]+:[0-9]+: (warning|error):" "$TMP" || true)

if [ -n "$HITS" ]; then
  echo "$HITS"
  echo
  echo "lint: $(echo "$HITS" | wc -l | tr -d ' ') finding(s) in project code"
  exit 1
fi

if grep -q '\[FAILED\]' "$TMP"; then
  echo "build failed — see full output: tail -50 $TMP (path persisted until shell exit)"
  cp "$TMP" /tmp/lint_build.log
  echo "copy saved at /tmp/lint_build.log"
  exit 1
fi

echo "lint: clean"
