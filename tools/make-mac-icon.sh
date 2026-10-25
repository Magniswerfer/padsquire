#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Builds the macOS app icon, src/padsquire/data/padsquire.icns, from tools/mac-app-icon.svg: the
# logo (src/padsquire/data/padsquire.svg) in white on a rounded square in PadSquire's blue, on
# Apple's 1024 px grid (an 824 px square with its margin), so macOS shows it as an app icon and
# Raycast, Spotlight and Finder all have one. Run it on a Mac after changing the SVG; the .icns
# ships in the package, so no Mac has to render it.
#
#   tools/make-mac-icon.sh
#
# Needs Google Chrome (headless, for a PNG with transparent corners; Quick Look paints them white)
# and macOS's own sips and iconutil.

set -eu
here=$(cd "$(dirname "$0")" && pwd)
chrome="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

"$chrome" --headless=new --disable-gpu --hide-scrollbars --window-size=1024,1024 \
    --default-background-color=00000000 --user-data-dir="$work/profile" \
    --screenshot="$work/icon.png" "file://$here/mac-app-icon.svg" 2>/dev/null
mkdir "$work/padsquire.iconset"
for size in 16 32 128 256 512; do
    sips -z $size $size "$work/icon.png" --out "$work/padsquire.iconset/icon_${size}x${size}.png" >/dev/null
    sips -z $((size * 2)) $((size * 2)) "$work/icon.png" --out "$work/padsquire.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$work/padsquire.iconset" -o "$here/../src/padsquire/data/padsquire.icns"
echo "Wrote src/padsquire/data/padsquire.icns"
