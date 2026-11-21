#!/usr/bin/env bash
# Renames the project from "lacuna" to a user-supplied name.
# Run once after cloning the template, before your first commit.
#
# Usage:  bash shell/bootstrap.sh <new-name>
#         bash shell/bootstrap.sh myproject
#
# The script rewrites file contents and renames the source directory in-place.
# It is idempotent when re-run with the same name, and safe to abort mid-run
# because it validates inputs before touching anything.

set -euo pipefail

# ── helpers ──────────────────────────────────────────────────────────────────

usage() {
    echo "Usage: $0 <new-project-name>"
    echo ""
    echo "  <new-project-name>  lowercase letters, digits, and hyphens only"
    echo "                      (e.g. my-project, foobar, cool-tool)"
    exit 1
}

die() { echo "error: $1" >&2; exit 1; }

# Portable in-place sed (BSD/macOS vs GNU)
sed_i() {
    if sed --version 2>/dev/null | grep -q GNU; then
        sed -i "$@"
    else
        sed -i '' "$@"
    fi
}

# ── argument validation ───────────────────────────────────────────────────────

[[ $# -eq 1 ]] || usage

NEW_NAME="$1"

# Must be a valid Python project name: lowercase letters, digits, hyphens
[[ "$NEW_NAME" =~ ^[a-z][a-z0-9-]*$ ]] || \
    die "name must start with a lowercase letter and contain only lowercase letters, digits, and hyphens"

[[ "$NEW_NAME" != "lacuna" ]] || die "new name must differ from the current name (lacuna)"

OLD_NAME="lacuna"
OLD_PKG="lacuna"

# Python package name: hyphens → underscores
NEW_PKG="${NEW_NAME//-/_}"

# Upper-case form for the settings env prefix: LACUNA_ → MY_TOOL_
# (tr, not ${VAR^^}: macOS ships bash 3.2)
OLD_UPPER="LACUNA"
NEW_UPPER="$(echo "$NEW_PKG" | tr '[:lower:]' '[:upper:]')"

# Title-case: capitalise first letter of each hyphen-separated word, rejoin with hyphen
# lacuna → Lacuna,  my-tool → My-Tool
OLD_TITLE="Lacuna"
NEW_TITLE="$(echo "$NEW_NAME" | awk -F'-' '{for(i=1;i<=NF;i++) $i=toupper(substr($i,1,1)) substr($i,2); OFS="-"; print}')"

# ── locate repo root ──────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

SRC_OLD="$ROOT/src/$OLD_PKG"
SRC_NEW="$ROOT/src/$NEW_PKG"

[[ -d "$SRC_OLD" ]] || die "source directory $SRC_OLD not found — already renamed?"

echo "Renaming '$OLD_NAME' → '$NEW_NAME' (package: $OLD_PKG → $NEW_PKG)"
echo ""

# ── files that contain text to replace ───────────────────────────────────────

TEXT_FILES=(
    "$ROOT/pyproject.toml"
    "$ROOT/README.md"
    "$ROOT/CLAUDE.md"
    "$ROOT/AGENTS.md"
    "$ROOT/.env.example"
    "$ROOT/.envrc"
)

# Python sources, plus the docs, workflows and agent files that name the project
while IFS= read -r -d '' f; do
    TEXT_FILES+=("$f")
done < <(find "$ROOT/src" "$ROOT/tests" -name "*.py" -print0)
while IFS= read -r -d '' f; do
    TEXT_FILES+=("$f")
done < <(find "$ROOT/docs" "$ROOT/.github" "$ROOT/.claude" -type f \
    \( -name "*.md" -o -name "*.yml" -o -name "*.yaml" \) -print0 2>/dev/null)

# ── text replacement ──────────────────────────────────────────────────────────
# Order matters: most-specific patterns first to avoid double-substitution.

for f in "${TEXT_FILES[@]}"; do
    [[ -f "$f" ]] || continue

    # 1. Upper-case env prefix  (LACUNA_LOG_LEVEL → MY_TOOL_LOG_LEVEL)
    # 2. Title-case references  (Lacuna → My-Tool)
    # 3. Package/module name  (lacuna → new_pkg, covers imports, paths, pyproject fields)
    # Left exactly as templated: the pyproject authors block, and any line naming the
    # template's GitHub owner or email (lacuna-core/lacuna_template, lacuna.core@…).
    sed_i -e '/^authors/,/^\]/b' -e '/lacuna[.-]core/b' \
        -e "s/${OLD_UPPER}/${NEW_UPPER}/g" \
        -e "s/${OLD_TITLE}/${NEW_TITLE}/g" \
        -e "s/${OLD_PKG}/${NEW_PKG}/g" "$f"

    # 3. If the project name uses hyphens, fix the pyproject `name` field and doc references
    #    that should use the hyphenated form (already replaced to underscored in step 2).
    if [[ "$NEW_NAME" != "$NEW_PKG" ]]; then
        sed_i "s/^name = \"${NEW_PKG}\"$/name = \"${NEW_NAME}\"/" "$f"
        sed_i "s/ ${NEW_PKG}\b/ ${NEW_NAME}/g" "$f"
    fi
done

# ── remove environment caches (contain absolute paths, must be rebuilt) ───────

rm -rf "$ROOT/.venv" "$ROOT/.mypy_cache" "$ROOT/.ruff_cache" "$ROOT/.pytest_cache" "$ROOT/.coverage"
find "$ROOT/src" "$ROOT/tests" -type d -name __pycache__ -prune -exec rm -rf {} +

# ── rename source directory ───────────────────────────────────────────────────

if [[ "$OLD_PKG" != "$NEW_PKG" ]]; then
    mv "$SRC_OLD" "$SRC_NEW"
    echo "Moved: src/$OLD_PKG/ → src/$NEW_PKG/"
fi

# ── self-destruct ─────────────────────────────────────────────────────────────

rm -- "${BASH_SOURCE[0]}"
rmdir "$ROOT/shell" 2>/dev/null || true  # remove dir only if now empty

# ── done ─────────────────────────────────────────────────────────────────────

echo ""
echo "Done. Next steps:"
echo "  1. Review the changes:  git diff"
echo "  2. Install deps:        uv sync --dev"
echo "  3. Run tests:           uv run pytest"
echo "  4. Commit:              git add -A && git commit -m 'chore: rename project to $NEW_NAME'"
