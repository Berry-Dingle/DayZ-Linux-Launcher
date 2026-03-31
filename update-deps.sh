#!/usr/bin/env bash
# Must be Bash ≥4
set -euo pipefail

PROJECT_ROOT="$(pwd)"

SPEC_FILE=$(find . -maxdepth 1 -name "*.spec" | head -n1)
if [ -z "${SPEC_FILE:-}" ]; then
    echo "❌ No .spec file found in $(pwd). Aborting."
    exit 1
fi

# Fedora Package Mapping
declare -A FEDORA_NAME_MAP=(
    # GUI / GTK ecosystem
    ["PyGObject"]="python3-gobject"
    ["pycairo"]="python3-cairo"
    ["Pillow"]="python3-pillow"

    # Networking / HTTP
    ["requests"]="python3-requests"
    ["urllib3"]="python3-urllib3"

    # Date / Time
    ["python-dateutil"]="python3-dateutil"
    ["pytz"]="python3-pytz"

    # Parsing / Serialization
    ["PyYAML"]="python3-pyyaml"
    ["toml"]="python3-toml"
    ["lxml"]="python3-lxml"

    # CLI / Utility
    ["click"]="python3-click"
    ["rich"]="python3-rich"

    # Scientific / Math
    ["numpy"]="python3-numpy"
    ["scipy"]="python3-scipy"
    ["matplotlib"]="python3-matplotlib"

    # Database
    ["psycopg2"]="python3-psycopg2"
    ["mysqlclient"]="python3-mysqlclient"

    # System
    ["pystray"]="python3-pystray"
)

VENV_DIR="${PROJECT_ROOT}/.venv"
if [ ! -d "${VENV_DIR:-}" ]; then
    echo "❌ No .venv found. Create it first."
    exit 1
fi

source "${VENV_DIR:-}/bin/activate"

echo "🔍 Detecting top-level installed packages..."

DEPS="$(pip list --local --not-required --format=freeze \
    | grep -vE '^(pip|setuptools|wheel|build|pytest)==' \
    | sed 's/==.*//' \
    | sort -u || true)"

: "${DEPS:=}"

if [ -z "$DEPS" ]; then
    echo "⚠ No top-level packages detected."
fi

echo "📄 Updating requirements.txt..."
echo "$DEPS" > requirements.txt

#########################################
# Update pyproject.toml
#########################################

PY_DEPS=$(echo "$DEPS" | sed 's/^/    "/; s/$/",/')

sed -i '/# BEGIN AUTO-DEPS/,/# END AUTO-DEPS/{
    /# BEGIN AUTO-DEPS/!{
        /# END AUTO-DEPS/!d
    }
}' pyproject.toml

TMP_FILE=$(mktemp)
printf "%s\n" "$PY_DEPS" > "$TMP_FILE"
sed -i '/# BEGIN AUTO-DEPS/r '"$TMP_FILE" pyproject.toml
rm -f "$TMP_FILE"

#########################################
# Update SPEC file
#########################################

SPEC_DEPS=""

while IFS= read -r pkg; do
    # DEPS is names-only (already stripped of versions)
    lower=$(echo "$pkg" | tr '[:upper:]' '[:lower:]')

    if [[ -n "${FEDORA_NAME_MAP[$pkg]:-}" ]]; then
        fedora_pkg="${FEDORA_NAME_MAP[$pkg]}"
    else
        fedora_pkg="python3-${lower}"
    fi

    SPEC_DEPS+="Requires:       ${fedora_pkg}\n"
done <<< "$DEPS"

sed -i '/# BEGIN AUTO-DEPS/,/# END AUTO-DEPS/{
    /# BEGIN AUTO-DEPS/!{
        /# END AUTO-DEPS/!d
    }
}' "${SPEC_FILE}"

TMP_FILE=$(mktemp)
printf '%b' "$SPEC_DEPS" > "$TMP_FILE"
sed -i '/# BEGIN AUTO-DEPS/r '"$TMP_FILE" "${SPEC_FILE}"
rm -f "$TMP_FILE"

deactivate

echo "✔ Dependencies synchronized successfully."
