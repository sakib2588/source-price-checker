#!/usr/bin/env bash
# Rebuild the README diagrams from their D2 sources. Needs d2 (https://d2lang.com) and rsvg-convert.
set -euo pipefail
cd "$(dirname "$0")"
D2="${D2:-d2}"
for name in how-it-works decision; do
  "$D2" "$name.d2" "$name.svg"
  rsvg-convert -w 1100 -b white "$name.svg" -o "$name.png"
  rm "$name.svg"
  echo "wrote docs/$name.png"
done
