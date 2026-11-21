#!/usr/bin/env bash
# Run after activating the pinned ESP-IDF environment; settings are provisioned separately.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${IDF_PATH:-}" ]]; then
    echo 'Activate ESP-IDF v6.1.1 with export.sh first.' >&2
    exit 1
fi
python "$project_dir/tools/apply_idf_patch.py" "$IDF_PATH"
build_root="$(mktemp -d "${TMPDIR:-/tmp}/sink-web-build.XXXXXXXX")"
echo "Building generic firmware in $build_root"
for variant in bluetooth i2s; do
    defaults="$project_dir/sdkconfig.defaults"
    if [[ "$variant" == i2s ]]; then
        defaults="$defaults;$project_dir/sdkconfig.pcm5102"
    fi
    idf.py -C "$project_dir" -B "$build_root/$variant" \
        -D SDKCONFIG="$build_root/$variant/sdkconfig" \
        -D SDKCONFIG_DEFAULTS="$defaults" build
done
python "$project_dir/../tools/publish_web_firmware.py" \
    --board "$(basename "$project_dir")" \
    --bluetooth "$build_root/bluetooth" --i2s "$build_root/i2s"
echo "Published frontend/firmware/$(basename "$project_dir"). Commit the board images and catalog manifest with this release."
