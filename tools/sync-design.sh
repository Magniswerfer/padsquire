#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Copies PadSquire's shared design system, the 3D controller model and the line drawings from the
# website repo into the settings app's static folder.
#
#   tools/sync-design.sh [path to the website repo]     (default: ../gsir-linux-website)
#
# The website repo is the source. Edit these files there, never here, and run this again after
# changing them: static/ds/, static/pad-model.js, static/vendor/three.js, static/models/ and the
# nova-2-lite-*.svg and icon.svg drawings in static/img/, and src/padsquire/data/padsquire.svg
# (the app icon, the same as icon.svg).

set -eu

here=$(cd "$(dirname "$0")/.." && pwd)
site=${1:-$here/../gsir-linux-website}
src=$site/public
dst=$here/src/padsquire/web/static

[ -f "$src/ds/tokens.css" ] || { echo "sync-design: no website repo at $site (expected public/ds/tokens.css)" >&2; exit 1; }

mkdir -p "$dst/ds" "$dst/vendor" "$dst/models" "$dst/img"
rsync -a --delete "$src/ds/" "$dst/ds/"
cp "$src/pad-model.js" "$dst/pad-model.js"
cp "$src/vendor/three.js" "$dst/vendor/three.js"     # pad-model.js imports ./vendor/three.js
cp "$src/models/nova-2-lite.glb" "$dst/models/nova-2-lite.glb"
rm -f "$dst"/img/nova-2-lite-*.svg
cp "$src"/img/nova-2-lite-*.svg "$src/img/icon.svg" "$dst/img/"
# The app-menu and dock icon: icon.svg with its colours written on the shapes and no <style>,
# which desktop icon renderers (KDE's among them) don't draw. Light colours: they read on both a
# light and a dark panel.
sed -e 's#<style>.*</style>##' \
    -e 's#class="body"#fill="\#fffffe" stroke="\#2c48a8" stroke-width="23.69" stroke-linejoin="round"#g' \
    -e 's#class="cap"#fill="\#2c48a8"#g' \
    "$src/img/icon.svg" > "$here/src/padsquire/data/padsquire.svg"
if grep -q 'class=\|<style' "$here/src/padsquire/data/padsquire.svg"; then
    echo "sync-design: icon.svg has changed shape; update the icon step in this script" >&2
    exit 1
fi
# A large bitmap of the icon: the app's window takes its icon from the page, and a browser
# turns an SVG favicon into a small, blurry bitmap for that.
if command -v magick >/dev/null 2>&1; then
    magick -background none -density 512 "$src/img/icon.svg" -resize 512x512 "$dst/img/icon-512.png"
else
    echo "sync-design: magick (ImageMagick) not found; static/img/icon-512.png not updated" >&2
fi

rev=$(git -C "$site" rev-parse --short HEAD 2>/dev/null || echo unknown)
dirty=$(git -C "$site" status --porcelain -- public 2>/dev/null | head -1)
echo "Copied from $site at $rev${dirty:+ (with uncommitted changes in public/)}."
