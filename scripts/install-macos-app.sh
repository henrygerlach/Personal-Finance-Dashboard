#!/bin/bash
# Creates two macOS app bundles so the dashboard can be started and stopped from
# Spotlight.
#
# Usage: ./scripts/install-macos-app.sh
# Then:  cmd+space -> "Personal-Finance-Dashboard" -> Enter
#
# The bundles contain only a stub; the actual logic stays in scripts/launcher.sh and
# scripts/stopper.sh inside the project. Changes there take effect immediately, without
# reinstalling.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${CD_APP_DIR:-$HOME/Applications}"

echo "Project: $REPO_DIR"
echo "Target:  $APP_DIR"

mkdir -p "$APP_DIR"

LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

# Without the compiler there is no usable bundle: a shell script as the executable
# would install, but macOS would deny it the project folder forever (see below).
fail_no_clang() {
  echo "Error: the stub could not be compiled. Install the Xcode command line tools:" >&2
  echo "  xcode-select --install" >&2
  exit 1
}

# make_bundle <app name> <script> <icon variant> <bundle id suffix>
make_bundle() {
  local name="$1" script="$2" variant="$3" suffix="$4"
  local app="$APP_DIR/$name.app"

  rm -rf "$app"
  mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"

  # The bundle only holds a stub calling the logic in the project directory. A script
  # copied into the bundle would go stale on every change without anyone noticing.
  #
  # The stub has to be a compiled binary, not a shell script. macOS protects ~/Documents,
  # where the project lives, and decides about that access by the code signature of the
  # running process. With a script as the bundle's executable that process is /bin/bash —
  # a system binary that has nothing to do with this app. macOS then never matches the
  # permission granted for the app, and never even asks: the app is denied silently and
  # does nothing at all when launched. A compiled executable carries the bundle's own
  # signature, so the permission applies to it and the question is asked once.
  local tmpc
  tmpc="$(mktemp -d)"
  cat > "$tmpc/main.c" <<'CSRC'
#include <fcntl.h>
#include <stdio.h>
#include <unistd.h>

/* Hands over to the script in the project directory. Only if it cannot be opened —
   the folder permission is missing — does it explain that, instead of dying mutely. */
int main(void) {
  const char *script = "@SCRIPT@";
  int fd = open(script, O_RDONLY);
  if (fd >= 0) {
    close(fd);
    execl("/bin/bash", "bash", script, (char *)0);
  }

  char msg[2048];
  snprintf(msg, sizeof msg,
           "display dialog \"macOS denies this app access to the project folder in "
           "Documents, so it cannot read its own script.\\n\\nSystem Settings -> "
           "Privacy & Security -> Full Disk Access -> + -> %s (in ~/Applications), "
           "then launch again.\" with title \"%s\" buttons {\"OK\"} default button "
           "\"OK\" with icon caution giving up after 300",
           "@NAME@", "@NAME@");
  execl("/usr/bin/osascript", "osascript", "-e", msg, (char *)0);
  return 1;
}
CSRC
  sed -i '' -e "s|@SCRIPT@|$REPO_DIR/scripts/$script|g" -e "s|@NAME@|$name|g" "$tmpc/main.c"

  if ! clang -O2 -o "$app/Contents/MacOS/$name" "$tmpc/main.c" 2>/dev/null; then
    fail_no_clang
  fi
  rm -rf "$tmpc"
  chmod +x "$app/Contents/MacOS/$name"

  # LSUIElement: both are launchers without a window of their own. Without this key a
  # bouncing dock icon would be left behind.
  cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$name</string>
  <key>CFBundleDisplayName</key><string>$name</string>
  <key>CFBundleIdentifier</key><string>local.comdirect.$suffix</string>
  <key>CFBundleExecutable</key><string>$name</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

  if command -v iconutil >/dev/null 2>&1 && command -v sips >/dev/null 2>&1; then
    local tmp
    tmp="$(mktemp -d)"
    python3 "$REPO_DIR/scripts/make-icon.py" "$tmp/icon.png" "$variant"
    mkdir -p "$tmp/AppIcon.iconset"
    for size in 16 32 64 128 256 512; do
      sips -z $size $size "$tmp/icon.png" \
        --out "$tmp/AppIcon.iconset/icon_${size}x${size}.png" >/dev/null
      local double=$((size * 2))
      sips -z $double $double "$tmp/icon.png" \
        --out "$tmp/AppIcon.iconset/icon_${size}x${size}@2x.png" >/dev/null
    done
    iconutil -c icns "$tmp/AppIcon.iconset" -o "$app/Contents/Resources/AppIcon.icns"
    rm -rf "$tmp"
  fi

  # Ad-hoc signature: gives the bundle a stable identity, which macOS needs to remember
  # a granted permission instead of asking again after every change.
  codesign --force --sign - "$app" >/dev/null 2>&1 || true

  # Register with Launch Services so Spotlight finds the app right away.
  [ -x "$LSREGISTER" ] && "$LSREGISTER" -f "$app" || true
  echo "Installed: $app"
}

make_bundle "Personal-Finance-Dashboard"      launcher.sh run  dashboard
make_bundle "Stop Personal-Finance-Dashboard" stopper.sh  stop stopper

echo
echo "Done."
echo "  Start: cmd+space -> \"Personal-Finance-Dashboard\""
echo "  Stop:  cmd+space -> \"Stop Personal-Finance-Dashboard\""
echo "Address: http://127.0.0.1:8000"
echo
echo "The project lives in ~/Documents, which macOS protects. Both apps need permission"
echo "for that folder, otherwise they cannot read their own scripts — they say so in a"
echo "dialog. Grant it under System Settings -> Privacy & Security -> Full Disk Access,"
echo "or let macOS ask again:"
echo "  tccutil reset SystemPolicyDocumentsFolder local.comdirect.dashboard"
echo "  tccutil reset SystemPolicyDocumentsFolder local.comdirect.stopper"
echo
echo "Log of both bundles: ~/Library/Logs/Comdirect-Dashboard.log"
