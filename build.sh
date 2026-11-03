#!/bin/zsh
# Build, sign and install:  ./build.sh
# Signs with the certificate from make_signing_cert.sh so macOS permissions survive rebuilds.
set -euo pipefail
cd "${0:A:h}"
NAME="DeathAdder V2 Local Signing"
APP="dist/DeathAdder V2.app"

if ! security find-certificate -c "$NAME" >/dev/null 2>&1; then
  echo "No signing certificate yet. Run ./make_signing_cert.sh first." >&2
  exit 1
fi

rm -rf build "$APP"
.venv/bin/python setup.py py2app >/dev/null
# Sign inside-out: codesign --deep trips over py2app's bundled libraries ("No such file or directory").
find "$APP/Contents" -type f \( -name "*.so" -o -name "*.dylib" \) -print0 | xargs -0 codesign --force --sign "$NAME"
find "$APP/Contents/Frameworks" -maxdepth 1 -name "*.framework" -print0 | xargs -0 -r codesign --force --sign "$NAME"
find "$APP/Contents/MacOS" -type f ! -name "DeathAdder V2" -print0 | xargs -0 -r codesign --force --sign "$NAME"
codesign --force --sign "$NAME" "$APP"
codesign --verify --deep --strict "$APP"

pkill -f "DeathAdder V2.app/Contents/MacOS" || true
rm -rf "/Applications/DeathAdder V2.app"
cp -R "$APP" /Applications/
open "/Applications/DeathAdder V2.app"
echo "Installed and started /Applications/DeathAdder V2.app"
