#!/bin/bash
# 雙擊開啟 PDF 工具（第一次會自動安裝所需套件）
cd "$(dirname "$0")" || exit 1
source tools/setup_mac.sh || exit 1

echo "正在開啟 PDF 工具…"
# 以獨立工作階段啟動，關閉這個終端機視窗也不會結束程式
"$VENV_PY" - "$ROOT/main.py" "$VENV/app.log" <<'PY'
import subprocess, sys
log = open(sys.argv[2], "w")
subprocess.Popen([sys.executable, sys.argv[1]], start_new_session=True,
                 stdin=subprocess.DEVNULL, stdout=log, stderr=log)
PY
echo "✅ 已開啟，可以關閉這個視窗。"
