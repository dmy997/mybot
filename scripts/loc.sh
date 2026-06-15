#!/usr/bin/env bash
# Count lines of code in mybot core (excludes tests, docs, assets, cache).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Directories counted as core code
CORE_DIRS=(
    agents
    config
    context
    core
    memory
    observability
    providers
    tools
    utils
    prompt_templates
    server_web
)

COUNTS=()

echo "mybot — Core Code Line Count"
echo "============================"
echo

total=0

for dir in "${CORE_DIRS[@]}"; do
    if [ -d "$dir" ]; then
        count=$(find "$dir" \
            -type f \
            \( -name "*.py" -o -name "*.html" -o -name "*.md" -o -name "*.css" -o -name "*.js" \) \
            ! -path "*/__pycache__/*" \
            -exec cat {} + 2>/dev/null | wc -l)
        printf "  %-20s %6d\n" "${dir}/" "$count"
        total=$((total + count))
    fi
done

# Top-level Python files (setup.py, etc., but not test files)
top_count=$(find . -maxdepth 1 \
    -type f \
    -name "*.py" \
    ! -name "setup.py" \
    -exec cat {} + 2>/dev/null | wc -l)
if [ "$top_count" -gt 0 ]; then
    printf "  %-20s %6d\n" "(root)" "$top_count"
    total=$((total + top_count))
fi

echo "                            ------"
printf "  %-20s %6d\n" "Total" "$total"
echo
echo "(excludes test/, docs, .env, __pycache__)"
