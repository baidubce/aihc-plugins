#!/usr/bin/env bash
#
# Copyright 2026 Baidu, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Adds Apache-2.0 license headers to source files that do not have one yet.
# Idempotent: files already containing the marker are skipped.
#
# Usage:
#   ./scripts/add-license-headers.sh          # apply
#   ./scripts/add-license-headers.sh --check  # CI mode, exit 1 if any file lacks header

set -euo pipefail

# Always scan from the repository root, regardless of the caller's cwd.
cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"

YEAR="2026"
HOLDER="Baidu, Inc."
MARKER="Licensed under the Apache License"
CHECK_ONLY=0

if [[ "${1:-}" == "--check" ]]; then
  CHECK_ONLY=1
fi

hash_header() {
  cat <<EOF
# Copyright ${YEAR} ${HOLDER} All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
EOF
}

collect_files() {
  find . \
    -path ./.git -prune -o \
    -path ./vendor -prune -o \
    -path ./node_modules -prune -o \
    -path ./.venv -prune -o \
    -type f \( -name '*.py' -o -name '*.sh' -o -name '*.yaml' -o -name '*.yml' \) \
    -print0
}

if [[ "${CHECK_ONLY}" -eq 1 ]]; then
  missing=()
  while IFS= read -r -d '' file; do
    grep -q "${MARKER}" "${file}" || missing+=("${file}")
  done < <(collect_files)

  if [[ ${#missing[@]} -gt 0 ]]; then
    printf 'Missing license header in %d file(s):\n' "${#missing[@]}"
    printf '  %s\n' "${missing[@]}"
    exit 1
  fi
  printf 'All source files carry a license header.\n'
  exit 0
fi

added=0
skipped=0

while IFS= read -r -d '' file; do
  if grep -q "${MARKER}" "${file}"; then
    skipped=$((skipped + 1))
    continue
  fi

  tmp="$(mktemp)"
  first_line="$(head -n 1 "${file}")"

  if [[ "${first_line}" == '#!'* ]]; then
    printf '%s\n' "${first_line}" > "${tmp}"
    hash_header >> "${tmp}"
    printf '\n' >> "${tmp}"
    tail -n +2 "${file}" >> "${tmp}"
  else
    hash_header > "${tmp}"
    printf '\n' >> "${tmp}"
    cat "${file}" >> "${tmp}"
  fi

  cat "${tmp}" > "${file}"
  rm -f "${tmp}"
  added=$((added + 1))
  printf 'added: %s\n' "${file}"
done < <(collect_files)

printf '\ndone. added=%d skipped=%d\n' "${added}" "${skipped}"
printf 'Note: .go/.js/.ts files use different comment syntax and are not handled.\n'
