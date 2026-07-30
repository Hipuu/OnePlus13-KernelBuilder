#!/usr/bin/env bash
# Local static validation for the OnePlus 13 kernel builder.
# Runs the checks documented in TESTING.md. No network access required
# except the optional asset-reachability check (skipped without curl).
set -uo pipefail

cd "$(dirname "$0")"

FAILED=0

pass() { echo "  ok    $1"; }
fail() { echo "  FAIL  $1"; FAILED=1; }
skip() { echo "  skip  $1"; }

WORKFLOW=.github/workflows/build-oneplus13-kernel.yml

# All six OP13 configs and their manifests
CONFIGS=(
  configs/OP13-6.6.89.json
  configs/OP13-6.6.118.json
  configs/OP13-6.6.66.json
  configs/OP13-6.6.30.json
  configs/OP13-CPH-6.6.89.json
  configs/OP13-CPH-6.6.56.json
)

MANIFESTS=(
  manifests/a16/oneplus_13_6.6.89_w.xml
  manifests/a16/oneplus_13_6.6.118_w.xml
  manifests/a15/oneplus_13_6.6.66_v.xml
  manifests/a15/oneplus_13_6.6.30_v.xml
  manifests/a15/oneplus_13_global_6.6.89_v.xml
  manifests/a15/oneplus_13_global_6.6.56_v.xml
)

mapfile -t ACTIONS < <(find .github/actions -name action.yml | sort)

echo "== YAML / JSON / XML parse =="
if command -v python3 >/dev/null 2>&1; then
    PYTHONUTF8=1 python3 - "$WORKFLOW" "${ACTIONS[@]}" <<'PY' || FAILED=1
import sys, yaml
for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as fh:
        yaml.safe_load(fh)
    print(f"  ok    {path}")
PY
    PYTHONUTF8=1 python3 - "${CONFIGS[@]}" "${MANIFESTS[@]}" <<'PY' || FAILED=1
import json, sys, xml.etree.ElementTree as ET
for path in sys.argv[1:]:
    if path.endswith('.json'):
        with open(path, encoding="utf-8") as fh:
            json.load(fh)
    else:
        ET.parse(path)
    print(f"  ok    {path}")
PY
else
    skip "python3 not found; YAML/JSON/XML parse not verified"
fi

echo
echo "== actionlint =="
# actionlint up to 1.7.7 still hard-codes a 10-input cap for
# workflow_dispatch. GitHub raised that long ago -- the documented limit is
# now 25 top-level inputs (65,535 characters of payload) -- and this
# workflow's 16 inputs dispatch fine. Suppress only that stale rule.
ACTIONLINT_IGNORE='maximum number of inputs for "workflow_dispatch" event'
if command -v actionlint >/dev/null 2>&1; then
    if actionlint -ignore "$ACTIONLINT_IGNORE"; then
        pass "actionlint"
    else
        fail "actionlint"
    fi
else
    skip "actionlint not installed (https://github.com/rhysd/actionlint)"
fi

echo
echo "== Workflow structure =="
grep -q "workflow_dispatch:" "$WORKFLOW" \
    && pass "workflow_dispatch trigger present" \
    || fail "workflow_dispatch trigger missing"

for action in "${ACTIONS[@]}"; do
    missing=()
    grep -q "^name:" "$action" || missing+=(name)
    grep -q "^description:" "$action" || missing+=(description)
    grep -q "^runs:" "$action" || missing+=(runs)
    if [ ${#missing[@]} -eq 0 ]; then
        pass "$action has name/description/runs"
    else
        fail "$action missing: ${missing[*]}"
    fi
done

echo
echo "== Nested local action references resolve =="
while read -r ref; do
    target="${ref#./}"
    if [ -f "$target/action.yml" ]; then
        pass "$ref"
    else
        fail "$ref (no $target/action.yml)"
    fi
done < <(grep -rhoE 'uses:[[:space:]]*\./[^[:space:]]+' .github \
         | sed -E 's/uses:[[:space:]]*//' | sort -u)

echo
echo "== OnePlus 13 config/manifest matrix =="
read_json_field() {
    if command -v jq >/dev/null 2>&1; then
        jq -r ".$2" "$1"
    else
        PYTHONUTF8=1 python3 -c \
            'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8")).get(sys.argv[2],""))' \
            "$1" "$2"
    fi
}

if command -v jq >/dev/null 2>&1 || command -v python3 >/dev/null 2>&1; then
    for cfg in "${CONFIGS[@]}"; do
        branch=$(read_json_field "$cfg" branch)
        manifest=$(read_json_field "$cfg" manifest)
        os_version=$(read_json_field "$cfg" os_version)

        [ "$branch" = "wild/sm8750" ] \
            && pass "$cfg: branch = wild/sm8750" \
            || fail "$cfg: branch != wild/sm8750 (got: $branch)"

        # Resolve manifest path: manifests/<os_version-lower>/<manifest>
        manifest_path="manifests/$(echo "$os_version" | tr '[:upper:]' '[:lower:]')/$manifest"
        if [ -f "$manifest_path" ]; then
            pass "$cfg: vendored manifest $manifest_path present"
        else
            fail "$cfg: vendored manifest $manifest_path missing"
        fi
    done
else
    skip "neither jq nor python3 found; config/manifest matrix not verified"
fi

echo
echo "== Manifest pins revisions =="
# Check the reference manifest; all six share identical toolchain pins.
REF_MANIFEST="${MANIFESTS[0]}"
for name in AnyKernel3 android_kernel_common_oneplus_sm8750 \
            kernel/prebuilts/build-tools clang/host/linux-x86; do
    if grep -F "$name\"" "$REF_MANIFEST" | grep -qE 'revision="[0-9a-f]{40}"'; then
        pass "$name pinned to a full SHA"
    else
        fail "$name not pinned to a full SHA"
    fi
done

echo
echo "== No boot image construction =="
# Permit kernel_modules in paths; ban boot/vendor_boot/vendor_dlkm/system_dlkm.
IMG_TOKENS='\b(mkbootimg|unpack_bootimg|mkdtboimg|avbtool)\b|boot\.img|vendor_boot|vendor_dlkm|system_dlkm'
IMG_HITS=$(grep -rniE "$IMG_TOKENS" .github configs manifests \
           | grep -viE 'does not (build|create)|kernel_modules' || true)
if [ -n "$IMG_HITS" ]; then
    echo "  matches found:"
    printf '%s\n' "$IMG_HITS" | sed 's/^/    /'
    fail "boot/DLKM image build commands present"
else
    pass "no mkbootimg/avbtool/boot.img/DLKM build commands"
fi

echo
echo "== Pinned toolchain-cache assets reachable =="
if command -v curl >/dev/null 2>&1 && [ "${SKIP_NETWORK:-0}" != "1" ]; then
    BASE=https://github.com/WildKernels/OnePlus_KernelSU_SUSFS/releases/download/toolchain-cache
    while read -r label rev; do
        code=$(curl -sIL -o /dev/null -w '%{http_code}' \
               --connect-timeout 20 "$BASE/$label-$rev.tar.gz")
        if [ "$code" = "200" ]; then
            pass "$label-$rev.tar.gz ($code)"
        else
            pcode=$(curl -sIL -o /dev/null -w '%{http_code}' \
                    --connect-timeout 20 "$BASE/$label-$rev.tar.gz.partaa")
            if [ "$pcode" = "200" ]; then
                pass "$label-$rev.tar.gz.partaa ($pcode, split asset)"
            else
                fail "$label-$rev.tar.gz unreachable (single=$code part=$pcode)"
            fi
        fi
    done <<EOF
AnyKernel3 $(grep 'name="AnyKernel3"' "$REF_MANIFEST" | grep -oE 'revision="[0-9a-f]{40}"' | cut -d'"' -f2)
build-tools $(grep 'name="kernel/prebuilts/build-tools"' "$REF_MANIFEST" | grep -oE 'revision="[0-9a-f]{40}"' | cut -d'"' -f2)
clang $(grep 'clang/host/linux-x86' "$REF_MANIFEST" | grep -oE 'revision="[0-9a-f]{40}"' | cut -d'"' -f2)
EOF
else
    skip "network check disabled or curl missing"
fi

echo
echo "== Required files tracked by git =="
if git rev-parse --git-dir >/dev/null 2>&1; then
    for f in "$WORKFLOW" "${CONFIGS[@]}" "${MANIFESTS[@]}" "${ACTIONS[@]}"; do
        if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
            pass "tracked: $f"
        else
            fail "untracked: $f (a fresh checkout would not have it)"
        fi
    done
else
    skip "not a git repository"
fi

echo
if [ "$FAILED" -eq 0 ]; then
    echo "All validation checks passed."
else
    echo "Validation FAILED."
fi
exit "$FAILED"
