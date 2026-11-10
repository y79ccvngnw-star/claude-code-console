#!/bin/bash
# 构建 dist/Claude控制台.app（macOS）
# 自包含：把 console.py 和图标打进 app，之后可以单独拖到 Dock。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/dist/Claude控制台.app"
CONTENTS="$APP/Contents"
ICON_SRC="$HERE/assets/icon-1024.png"

# 仓库以纯文本保存图标（.b64）；首次构建时还原出 PNG，生成物不入库
if [ ! -f "$ICON_SRC" ] && [ -f "$ICON_SRC.b64" ]; then
  if ! /usr/bin/base64 -D -i "$ICON_SRC.b64" -o "$ICON_SRC" 2>/dev/null; then
    /usr/bin/base64 -d "$ICON_SRC.b64" > "$ICON_SRC"
  fi
fi

if [ ! -f "$HERE/console.py" ]; then
  echo "找不到 console.py" >&2
  exit 1
fi
if [ ! -f "$ICON_SRC" ]; then
  echo "找不到 assets/icon-1024.png" >&2
  exit 1
fi

rm -rf "$APP"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"

# 自包含源码
cp "$HERE/console.py" "$CONTENTS/Resources/console.py"

cat > "$CONTENTS/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleName</key>
	<string>Claude控制台</string>
	<key>CFBundleDisplayName</key>
	<string>Claude 控制台</string>
	<key>CFBundleIdentifier</key>
	<string>local.panel.console</string>
	<key>CFBundleVersion</key>
	<string>1.0</string>
	<key>CFBundleShortVersionString</key>
	<string>1.0</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleSignature</key>
	<string>????</string>
	<key>CFBundleExecutable</key>
	<string>launcher</string>
	<key>CFBundleIconFile</key>
	<string>icon.icns</string>
	<key>LSMinimumSystemVersion</key>
	<string>10.15</string>
	<key>LSUIElement</key>
	<true/>
	<key>NSHighResolutionCapable</key>
	<true/>
</dict>
</plist>
PLIST

printf 'APPL????' > "$CONTENTS/PkgInfo"

cat > "$CONTENTS/MacOS/launcher" <<'LAUNCHER'
#!/bin/bash
# 从 Dock / Finder 启动面板：服务没跑就后台启动，然后打开浏览器。
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
RES="$(cd "$HERE/../Resources" && pwd)"
PORT="${CCP_PORT:-7788}"
URL="http://127.0.0.1:$PORT/"

alive() {
  /usr/bin/curl -fsS --max-time 1 "$URL" >/dev/null 2>&1
}

if ! alive; then
  cd "$RES" || exit 1
  /usr/bin/nohup /usr/bin/python3 "$RES/console.py" > /tmp/ccp.log 2>&1 &
  for _ in $(seq 1 40); do
    /bin/sleep 0.25
    alive && break
  done
fi

if ! alive; then
  /usr/bin/osascript -e 'display notification "启动失败，日志在 /tmp/ccp.log" with title "Claude 控制台"' >/dev/null 2>&1
  exit 1
fi

# 测试时可用 CCP_NO_OPEN=1 或 --no-open 跳过打开浏览器
NO_OPEN="${CCP_NO_OPEN:-0}"
[ "${1:-}" = "--no-open" ] && NO_OPEN=1
if [ "$NO_OPEN" != "1" ]; then
  /usr/bin/open "$URL"
fi
exit 0
LAUNCHER

chmod +x "$CONTENTS/MacOS/launcher"

# 生成 icns（iconutil 要求目录名以 .iconset 结尾）
TMP_ICONSET="$(mktemp -d /tmp/panel-iconset.XXXXXX)"
ICONSET="$TMP_ICONSET/panel.iconset"
mkdir -p "$ICONSET"
trap 'rm -rf "$TMP_ICONSET"' EXIT
while read -r size name; do
  sips -z "$size" "$size" "$ICON_SRC" --out "$ICONSET/$name" >/dev/null
done <<'SIZES'
16 icon_16x16.png
32 icon_16x16@2x.png
32 icon_32x32.png
64 icon_32x32@2x.png
128 icon_128x128.png
256 icon_128x128@2x.png
256 icon_256x256.png
512 icon_256x256@2x.png
512 icon_512x512.png
1024 icon_512x512@2x.png
SIZES
iconutil -c icns "$ICONSET" -o "$CONTENTS/Resources/icon.icns"

# 本地 ad-hoc 签名，避免 LaunchServices 缓存问题；失败也不影响使用
xattr -cr "$APP" 2>/dev/null || true
codesign --force --deep --sign - "$APP" 2>/dev/null || true

echo "built: $APP"
