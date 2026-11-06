#!/bin/bash
# Puts a private copy of Python inside the app, so nobody has to install one.
# Downloads the pinned standalone build, checks its hash, removes what the engine
# never uses, and merges the architectures into one universal runtime.
#   ./bundle-python.sh <dest folder> <arch> [arch...]     arch: arm64 | x86_64
set -euo pipefail

VERSION=3.13.18
RELEASE=20261026
BASE="https://github.com/astral-sh/python-build-standalone/releases/download/$RELEASE"

HERE="$(cd "$(dirname "$0")" && pwd)"
CACHE="$HERE/.python-cache"
DEST="$1"; shift
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$CACHE"

first=""
for arch in "$@"; do
  case "$arch" in
    arm64)  triple=aarch64-apple-darwin; sha=8392b6ea51d94391befb2c31ebe75463f045d0b69dfcf5e1bb3234e1f5fa3405 ;;
    x86_64) triple=x86_64-apple-darwin;  sha=7412c19d896c99431b74a18a5643e110449a7f9590fbab179c3e5ab27b9aa9e3 ;;
    *) echo "unknown arch: $arch"; exit 1 ;;
  esac
  file="cpython-$VERSION+$RELEASE-$triple-install_only_stripped.tar.gz"
  if [[ ! -f "$CACHE/$file" ]]; then
    curl -fsSL --retry 3 -o "$CACHE/$file.part" "$BASE/cpython-$VERSION%2B$RELEASE-$triple-install_only_stripped.tar.gz"
    mv "$CACHE/$file.part" "$CACHE/$file"
  fi
  echo "$sha  $CACHE/$file" | shasum -a 256 -c --quiet

  mkdir -p "$WORK/$arch"
  tar -xzf "$CACHE/$file" -C "$WORK/$arch"
  py="$WORK/$arch/python"
  major="${VERSION%.*}"
  rm -rf "$py/include" "$py/share" "$py/lib/pkgconfig" "$py"/lib/libtcl* "$py"/lib/libtk* \
         "$py"/lib/tcl* "$py"/lib/tk* "$py"/lib/itcl* "$py"/lib/thread*
  find "$py/bin" -mindepth 1 ! -name "python*" -delete
  rm -f "$py/bin/python$major-config" "$py/bin/python3-config"
  lib="$py/lib/python$major"
  rm -rf "$lib/test" "$lib/idlelib" "$lib/tkinter" "$lib/turtledemo" "$lib/ensurepip" \
         "$lib/pydoc_data" "$lib/venv" "$lib"/config-* "$lib/site-packages"/*
  rm -f "$lib"/lib-dynload/_tkinter* "$lib"/lib-dynload/_test* "$lib"/lib-dynload/xx*

  if [[ -z "$first" ]]; then
    first="$py"
  else
    # same layout, different chip: join every binary into a universal one
    (cd "$py" && find . -type f) | while read -r f; do
      if file -b "$first/$f" | grep -q "Mach-O"; then
        lipo -create "$first/$f" "$py/$f" -output "$first/$f.fat" && mv "$first/$f.fat" "$first/$f"
      fi
    done
  fi
done

rm -rf "$DEST"
mkdir -p "$(dirname "$DEST")"
cp -R "$first" "$DEST"
