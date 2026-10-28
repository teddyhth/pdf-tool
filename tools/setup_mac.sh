# 由「開啟_Mac.command」與「打包_Mac.command」共用（以 source 載入）。
# 找到含 Tk 8.6 以上的 Python，建立 .venv-mac 並安裝相依套件。
# 成功後 VENV_PY 指向虛擬環境的 python；失敗則 return 1。

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv-mac"
VENV_PY="$VENV/bin/python"

fail() {
  echo ""
  echo "❌ $1"
  read -r -p "按 Enter 關閉…" _
  return 1
}

# macOS 內建的 Tk 8.5 很不穩定，必須 8.6 以上
has_tk() {
  "$1" -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 9) and tkinter.TkVersion >= 8.6 else 1)" >/dev/null 2>&1
}

find_python() {
  local v c
  for v in 3.12 3.13 3.11 3.14 3.10 3.15; do
    for c in "/Library/Frameworks/Python.framework/Versions/$v/bin/python3" \
             "/opt/homebrew/bin/python$v" "/usr/local/bin/python$v"; do
      [ -x "$c" ] && has_tk "$c" && { echo "$c"; return 0; }
    done
  done
  c="$(command -v python3)"
  [ -n "$c" ] && has_tk "$c" && { echo "$c"; return 0; }
  return 1
}

# Homebrew 的 Python 預設不含 Tkinter，可補裝 python-tk@版本
install_brew_tk() {
  local brew v
  brew="$(command -v brew || ls /opt/homebrew/bin/brew /usr/local/bin/brew 2>/dev/null | head -1)"
  [ -n "$brew" ] || return 1
  for v in 3.12 3.13 3.11 3.14 3.10 3.15; do
    [ -x "$("$brew" --prefix)/bin/python$v" ] || continue
    echo "偵測到 Homebrew 的 Python $v，但缺少圖形介面元件（Tkinter）。"
    read -r -p "要自動執行「brew install python-tk@$v」安裝嗎？[Y/n] " ans || return 1
    case "$ans" in [nN]*) return 1 ;; esac
    "$brew" install "python-tk@$v"
    return $?
  done
  return 1
}

if [ ! -x "$VENV_PY" ] || ! has_tk "$VENV_PY"; then
  echo "第一次使用，正在建立執行環境…"
  BASE_PY="$(find_python)"
  if [ -z "$BASE_PY" ] && install_brew_tk; then
    BASE_PY="$(find_python)"
  fi
  if [ -z "$BASE_PY" ]; then
    open "https://www.python.org/downloads/macos/"
    fail "找不到可用的 Python。請從剛開啟的網頁下載安裝 Python 3.12（macOS 64-bit universal2 installer），裝好後再雙擊一次。"
    return 1
  fi
  echo "使用 Python：$BASE_PY"
  rm -rf "$VENV"
  "$BASE_PY" -m venv "$VENV" || { fail "建立虛擬環境失敗"; return 1; }
fi

# requirements.txt 有變動才重新安裝
if ! cmp -s "$ROOT/requirements.txt" "$VENV/installed-requirements.txt"; then
  echo "正在安裝相依套件（只需要一次，約 1 分鐘）…"
  "$VENV_PY" -m pip install --disable-pip-version-check -q -r "$ROOT/requirements.txt" \
    || { fail "安裝相依套件失敗，請確認網路連線"; return 1; }
  cp "$ROOT/requirements.txt" "$VENV/installed-requirements.txt"
fi
