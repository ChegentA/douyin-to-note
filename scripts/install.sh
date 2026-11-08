#!/usr/bin/env bash
# douyin-video-skill 安装脚本
# 适用：macOS / Linux / WSL / Git Bash(MSYS) / Cygwin
# 用法：bash scripts/install.sh [--force]
#   --force  即使已装过 playwright-core 也重新安装
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

echo "=========================================="
echo "  douyin-video-skill 安装"
echo "  $SKILL_DIR"
echo "=========================================="

case "$(uname -s)" in
  *Microsoft*|*MINGW*|*MSYS*|*CYGWIN*) IS_WINDOWS=1 ;;
  *)
    if [ -n "${WSL_DISTRO_NAME:-}" ] || uname -r | grep -qi microsoft; then
      IS_WINDOWS=1
    else
      IS_WINDOWS=0
    fi
    ;;
esac

# WSL 的非登录 shell 不会自动继承 Windows PATH，这里从注册表侧补一次
import_windows_path() {
  command -v powershell.exe >/dev/null 2>&1 || return 0
  local winpath converted
  winpath="$(powershell.exe -NoProfile -Command "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')" 2>/dev/null | tr -d '\r')"
  [ -n "$winpath" ] || return 0
  converted="$(printf '%s' "$winpath" | tr ';' '\n' | sed -e 's#^\([A-Za-z]\):\\#/mnt/\L\1/#' -e 's#\\#/#g' | grep -v '^$' | tr '\n' ':')"
  export PATH="$PATH:$converted"
}

if [ "$IS_WINDOWS" = 1 ] && { ! command -v node.exe >/dev/null 2>&1 || ! command -v python.exe >/dev/null 2>&1; }; then
  import_windows_path
fi

# Windows 上（WSL / Git Bash）优先调用 Windows 原生 python.exe / node.exe
if [ "$IS_WINDOWS" = 1 ]; then
  PY_BIN="$(command -v python.exe || command -v python3.exe || command -v python3 || command -v python || true)"
  NODE_BIN="$(command -v node.exe || command -v node || true)"
else
  PY_BIN="$(command -v python3 || command -v python || true)"
  NODE_BIN="$(command -v node || true)"
fi
NPM_BIN="$(command -v npm || true)"

die() { echo "[FAIL] $*" >&2; exit 1; }
ok() { echo "[OK]   $*"; }
warn() { echo "[WARN] $*"; }

# 把 POSIX 路径转成 Windows 路径，供 python.exe / node.exe 使用
win_path() {
  if command -v wslpath >/dev/null 2>&1; then
    wslpath -w "$1"
  elif command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$1"
  else
    printf '%s' "$1"
  fi
}

native_path() {
  if [ "$IS_WINDOWS" = 1 ]; then win_path "$1"; else printf '%s' "$1"; fi
}

version_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]; }

# ---------- 1. Python ----------
echo ""
echo "[1/4] 检查 Python 与 requests"
[ -n "$PY_BIN" ] || die "未找到 Python，请先安装 Python 3.8+ 并加入 PATH"
PY_VER="$("$PY_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null | tr -d '\r' || true)"
[ -n "$PY_VER" ] || die "无法运行 $PY_BIN"
version_ge "$PY_VER" "3.8" || die "Python 版本过低：$PY_VER（需要 >= 3.8）"
ok "Python $PY_VER ($PY_BIN)"

if ! "$PY_BIN" -c "import requests" >/dev/null 2>&1; then
  echo "      安装 requests ..."
  "$PY_BIN" -m pip install --quiet --disable-pip-version-check requests || die "requests 安装失败"
fi
ok "requests $("$PY_BIN" -c 'import requests; print(requests.__version__)')"

# ---------- 2. Node ----------
echo ""
echo "[2/4] 检查 Node"
[ -n "$NODE_BIN" ] || die "未找到 Node.js，请安装 Node 18+ 并加入 PATH"
NODE_VER="$("$NODE_BIN" -p 'process.versions.node' 2>/dev/null | tr -d '\r' || true)"
[ -n "$NODE_VER" ] || die "无法运行 $NODE_BIN"
version_ge "$NODE_VER" "18.0.0" || die "Node 版本过低：$NODE_VER（playwright-core 需要 >= 18）"
ok "Node $NODE_VER ($NODE_BIN)"

# ---------- 3. playwright-core ----------
echo ""
echo "[3/4] 安装 playwright-core（不下载浏览器内核，复用本机 Edge/Chrome）"
PWCORE="$SKILL_DIR/node_modules/playwright-core"
# 必须显式 --prefix：否则 npm 会向上找到 .cc-switch / 用户目录的 package.json，把包装到库外
if [ "$IS_WINDOWS" = 1 ]; then
  NPM_PREFIX="$(win_path "$SKILL_DIR")"
else
  NPM_PREFIX="$SKILL_DIR"
fi
NPM_INSTALL_ARGS=(install --prefix "$NPM_PREFIX" --no-save --no-package-lock --no-audit --no-fund playwright-core)

if [ -d "$PWCORE" ] && [ "$FORCE" = 0 ]; then
  ok "已存在 $PWCORE（如需重装：bash scripts/install.sh --force）"
else
  if [ "$IS_WINDOWS" = 1 ]; then
    NPM_CLI="$(dirname "$NODE_BIN")/node_modules/npm/bin/npm-cli.js"
    if [ -f "$NPM_CLI" ]; then
      ( cd "$SKILL_DIR" && "$NODE_BIN" "$(win_path "$NPM_CLI")" "${NPM_INSTALL_ARGS[@]}" ) \
        || die "playwright-core 安装失败（npm-cli：$NPM_CLI）"
    else
      [ -n "$NPM_BIN" ] || die "未找到 npm，请安装 Node.js 完整包"
      ( cd "$SKILL_DIR" && "$NPM_BIN" "${NPM_INSTALL_ARGS[@]}" ) || die "playwright-core 安装失败"
    fi
  else
    [ -n "$NPM_BIN" ] || die "未找到 npm，请安装 Node.js 完整包"
    ( cd "$SKILL_DIR" && "$NPM_BIN" "${NPM_INSTALL_ARGS[@]}" ) || die "playwright-core 安装失败"
  fi
  [ -d "$PWCORE" ] || die "安装后仍未找到 $PWCORE"
  ok "playwright-core 已安装到 $PWCORE"
fi

# 若已通过环境变量 / --playwright-module 指定了模块，这里只做提示
if [ -n "${DOUYIN_PLAYWRIGHT_MODULE:-}" ]; then
  warn "检测到 DOUYIN_PLAYWRIGHT_MODULE=$DOUYIN_PLAYWRIGHT_MODULE，运行时会优先使用它"
fi

# ---------- 4. 浏览器 + Key 自检 ----------
echo ""
echo "[4/4] 自检"
FETCH_JS="$(native_path "$SCRIPT_DIR/fetch_douyin_detail.js")"
CHECK_OUT="$("$NODE_BIN" "$FETCH_JS" --check 2>&1 || true)"
echo "$CHECK_OUT" | grep -q "CHECK ok" && ok "浏览器可用：$(echo "$CHECK_OUT" | grep "CHECK ok" | head -n1)" \
  || warn "浏览器自检未通过：$(echo "$CHECK_OUT" | tail -n2)"

ANALYZER="$(native_path "$SCRIPT_DIR/douyin_analyzer.py")"
# 打印用：Windows 下让 python 走 Windows 路径，避免 /mnt/c 与 C:\ 混用
PY_CMD="$PY_BIN"
if [ "$IS_WINDOWS" = 1 ]; then PY_CMD="$(win_path "$PY_BIN")"; fi
KEY_OUT="$("$PY_BIN" "$ANALYZER" --check-key 2>&1 || true)"
echo "$KEY_OUT" | grep -qi "来源" && ok "API Key：$(echo "$KEY_OUT" | grep -i "来源" | head -n1)" \
  || warn "尚未配置 SiliconFlow API Key"

echo ""
echo "=========================================="
echo "  安装完成"
echo "=========================================="
echo ""
echo "配置 Key（可选，二选一）："
echo "  1) 交互输入，不落命令历史："
echo "     \"$PY_CMD\" \"$ANALYZER\" --set-key"
echo "  2) 设置环境变量 SILICONFLOW_API_KEY"
echo ""
echo "配置笔记目录（由你决定，AI 会把笔记写到这里）："
echo "  \"$PY_CMD\" \"$ANALYZER\" --set-notes-dir \"D:\\notes\\douyin\""
echo "  也可以用环境变量 DOUYIN_NOTES_DIR 覆盖"
echo ""
echo "自检命令："
echo "  \"$PY_CMD\" \"$ANALYZER\" --check-key"
echo ""
echo "分析视频："
echo "  \"$PY_CMD\" \"$ANALYZER\" <抖音链接>"
echo ""
