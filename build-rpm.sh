#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(pwd)"
APP_NAME="$(basename "$PROJECT_ROOT")"

echo "🔄 Syncing dependencies..."
./update-deps.sh

echo "📦 Building Python package..."

VENV_DIR="${PROJECT_ROOT}/venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "❌ venv not found at: $VENV_DIR"
    echo "   Create it first (or re-run the scaffold script)."
    exit 1
fi

# Use the project's venv so 'build' is guaranteed available
source "$VENV_DIR/bin/activate"
python3 -m build
deactivate

echo "📂 Preparing rpmbuild tree..."
rpmdev-setuptree >/dev/null 2>&1 || true

TARBALL=$(ls dist/*.tar.gz | head -n1)

cp "$TARBALL" ~/rpmbuild/SOURCES/
cp ./*.spec ~/rpmbuild/SPECS/

echo "🏗 Building RPM..."
rpmbuild -ba ~/rpmbuild/SPECS/*.spec

echo "🔎 Running rpmlint..."
rpmlint ~/rpmbuild/SPECS/*.spec || true

echo "✅ RPM build complete."
