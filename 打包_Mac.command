#!/bin/bash
# 雙擊打包成 dist/PDFTool.app（之後可直接雙擊 App，不需終端機）
cd "$(dirname "$0")" || exit 1
source tools/setup_mac.sh || exit 1

echo "正在安裝 PyInstaller…"
"$VENV_PY" -m pip install --disable-pip-version-check -q pyinstaller || { fail "安裝 PyInstaller 失敗"; exit 1; }

echo "正在打包（約 1–2 分鐘）…"
"$VENV_PY" -m PyInstaller --noconfirm --clean --windowed --log-level WARN \
  --name PDFTool --osx-bundle-identifier com.local.pdftool \
  --distpath "$ROOT/dist" --workpath "$ROOT/build" --specpath "$ROOT/build" \
  "$ROOT/main.py" || { fail "打包失敗，請查看上方訊息"; exit 1; }

echo ""
echo "✅ 完成：dist/PDFTool.app（可拖到「應用程式」資料夾）"
open "$ROOT/dist"
read -r -p "按 Enter 關閉…" _
