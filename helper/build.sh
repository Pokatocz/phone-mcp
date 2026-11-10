#!/usr/bin/env bash
# Build helper/phone-mcp-helper.jar from helper/src. Needs a JDK (17+), curl and unzip.
# The Android API stubs and the d8 dexer are downloaded from Google on first use.
set -euo pipefail
cd "$(dirname "$0")"
TOOLS="${PHONE_MCP_TOOLS:-.tools}"
R8_VERSION=8.13.25
mkdir -p "$TOOLS" build/classes

if [ ! -f "$TOOLS/r8.jar" ]; then
  curl -fsSL -o "$TOOLS/r8.jar" "https://dl.google.com/android/maven2/com/android/tools/r8/$R8_VERSION/r8-$R8_VERSION.jar"
fi
if [ ! -f "$TOOLS/android.jar" ]; then
  curl -fsSL -o "$TOOLS/platform.zip" https://dl.google.com/android/repository/platform-35_r02.zip
  unzip -q -o -j "$TOOLS/platform.zip" '*/android.jar' -d "$TOOLS"
  rm "$TOOLS/platform.zip"
fi

rm -rf build/classes/* build/dex && mkdir -p build/dex
javac --release 11 -Xlint:-options -cp "$TOOLS/android.jar" -d build/classes src/*.java
java -cp "$TOOLS/r8.jar" com.android.tools.r8.D8 --release --min-api 30 --lib "$TOOLS/android.jar" \
  --output build/dex $(find build/classes -name '*.class')
# A jar with a fixed timestamp, so the same source always gives the same file.
rm -f phone-mcp-helper.jar
TZ=UTC touch -t 202001010000 build/dex/classes.dex
(cd build/dex && TZ=UTC zip -q -X ../../phone-mcp-helper.jar classes.dex)
ls -l phone-mcp-helper.jar
