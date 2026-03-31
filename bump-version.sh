#!/usr/bin/env bash
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: ./bump-version.sh NEW_VERSION"
    exit 1
fi

NEW_VERSION="$1"

# Read current version from pyproject.toml (first match)
OLD_VERSION="$(awk -F'"' '/^version = "/ {print $2; exit}' pyproject.toml)"

if [ -z "${OLD_VERSION:-}" ]; then
    echo "❌ Could not read OLD_VERSION from pyproject.toml"
    exit 1
fi

echo "🔄 Updating pyproject.toml..."
sed -i "s/^version = \".*\"/version = \"${NEW_VERSION}\"/" pyproject.toml

echo "🔄 Updating spec file..."
SPEC_FILE="$(ls -1 *.spec | head -n1)"
sed -i "s/^Version:.*/Version:        ${NEW_VERSION}/" "$SPEC_FILE"
sed -i "s/^Source0:.*%{name}-${OLD_VERSION}\.tar\.gz/Source0:        %{name}-${NEW_VERSION}.tar.gz/" "$SPEC_FILE"
sed -i "s/^%autosetup -n %{name}-${OLD_VERSION}/%autosetup -n %{name}-${NEW_VERSION}/" "$SPEC_FILE"

echo "✅ Version bumped from ${OLD_VERSION} to ${NEW_VERSION}"
