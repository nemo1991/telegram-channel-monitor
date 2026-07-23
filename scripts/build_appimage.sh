#!/usr/bin/env bash
# build_appimage.sh — 把 PyInstaller 产出的 dist/tgmonitor/ 包成 Linux AppImage
#
# 需先 uv run pyinstaller --clean tgmonitor.spec 完毕。
# 需装 appimagetool(本仓库的 .github/workflows/build.yml 在 ubuntu-latest
# runner 一次性 curl + chmod +x)。
# 需装 librsvg2-bin(提供 rsvg-convert)把 SVG icon 转 PNG — AppImage 标准
# 要求 .desktop 文件的 Icon= 字段指向 PNG(虽然现代桌面也接受 SVG,
# 但 appimagetool 在某些 distro 上的 desktop integration 仍要 PNG)。
#
# 产物:`tgmonitor-x86_64.AppImage`(单文件,可 chmod +x 直接跑)
set -euo pipefail

# ---- config ----
DIST=dist/tgmonitor
APPDIR=AppDir
OUT=tgmonitor-x86_64.AppImage
ICON_SVG=src/tgmonitor/resources/app_icon.svg
ICON_PNG=tgmonitor-256x256.png

if [[ ! -d "$DIST" ]]; then
    echo "❌ $DIST 不存在 — 请先跑 PyInstaller:"
    echo "    PYTHONPATH=src uv run pyinstaller --clean -y tgmonitor.spec"
    exit 1
fi

# ---- 清理 + 创建 AppDir ----
rm -rf "$APPDIR"
mkdir -p \
    "$APPDIR/usr/bin" \
    "$APPDIR/usr/lib" \
    "$APPDIR/usr/share/icons/hicolor/256x256/apps" \
    "$APPDIR/usr/share/applications"

# ---- 二进制 + 所有 deps 拷到 AppDir/usr/lib/ ----
cp -r "$DIST"/* "$APPDIR/usr/lib/"
# 把 launcher 移到 AppDir/usr/bin/(FSH / Linux Standard Base 习惯)
mv "$APPDIR/usr/lib/tgmonitor" "$APPDIR/usr/bin/tgmonitor"

# ---- icon ----
if command -v rsvg-convert >/dev/null 2>&1; then
    # SVG → 256x256 PNG(AppImage 标准 .desktop Icon 字段;也兼容 XDG)
    rsvg-convert -w 256 -h 256 "$ICON_SVG" -o "$ICON_PNG"
else
    echo "⚠️ rsvg-convert 缺失,librsvg2-bin 没装;fallback 用 SVG 拷贝"
    echo "   (某些 desktop environment / AppImageLauncher 不支持 SVG .desktop)"
    cp "$ICON_SVG" "$APPDIR/usr/share/icons/hicolor/256x256/apps/tgmonitor.svg"
fi

# 同时把 icon 放 AppDir 顶层(AppRun / desktop entry 找 Icon=tgmonitor 时)
# 现代 AppImage spec 允许 Icon= 是相对路径,这里两份都放:PNG 优先,SVG fallback
cp "$ICON_SVG" "$APPDIR/tgmonitor.svg"
[[ -f "$ICON_PNG" ]] && cp "$ICON_PNG" "$APPDIR/tgmonitor.png"

# 256x256 位置(桌面集成):
[[ -f "$ICON_PNG" ]] && cp "$ICON_PNG" "$APPDIR/usr/share/icons/hicolor/256x256/apps/tgmonitor.png" || true

# ---- .desktop entry ----
cat > "$APPDIR/tgmonitor.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Telegram Channel Monitor
GenericName=Telegram Channel Monitor
Comment=Monitor and export messages from Telegram channels
Exec=tgmonitor %u
Icon=tgmonitor
Categories=Network;Chat;Monitor;
Terminal=false
StartupNotify=true
StartupWMClass=tgmonitor
EOF

# ---- AppRun(让 AppDir 变成可执行 + 转发到 usr/bin/tgmonitor)----
cat > "$APPDIR/AppRun" <<'EOF'
#!/usr/bin/env bash
# AppRun — AppImage 入口点;转发 argv 到 AppDir 内二进制
exec "$(dirname "$(readlink -f "$0")")/usr/bin/tgmonitor" "$@"
EOF
chmod +x "$APPDIR/AppRun" "$APPDIR/usr/bin/tgmonitor"

# ---- 跑 appimagetool ----
if ! command -v appimagetool >/dev/null 2>&1; then
    echo "❌ appimagetool 不在 PATH"
    echo "   CI 里跑:curl -fsSL -o /usr/local/bin/appimagetool \\"
    echo "     https://github.com/AppImageCommunity/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage \\"
    echo "     && chmod +x /usr/local/bin/appimagetool"
    exit 1
fi

ARCH=x86_64 appimagetool "$APPDIR" "$OUT"

echo "✅ Built $OUT"
ls -lh "$OUT"
file "$OUT"