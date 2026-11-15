#!/usr/bin/env bash
# Render docs/images/poc/*.svg to PNG with headless Chrome (sized from each SVG's viewBox).
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)/docs/images/poc"
CHROME="${CHROME:-google-chrome}"
for svg in "$DIR"/*.svg; do
  png="${svg%.svg}.png"
  read -r w h < <(python3 -c "import re,sys;v=re.search(r'viewBox=\"0 0 ([\d.]+) ([\d.]+)\"',open(sys.argv[1]).read());print(int(float(v[1])),int(float(v[2]))+2)" "$svg")
  html="$(mktemp --suffix=.html)"
  printf '<html><body style="margin:0;background:#272822">%s</body></html>' "$(cat "$svg")" > "$html"
  "$CHROME" --headless=new --disable-gpu --no-sandbox --hide-scrollbars --force-device-scale-factor=1 \
    --window-size="$w,$h" --screenshot="$png" "file://$html" >/dev/null 2>&1
  rm -f "$html"
  echo "$(basename "$png") ${w}x${h}"
done
