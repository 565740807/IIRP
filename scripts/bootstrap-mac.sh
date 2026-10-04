#!/bin/sh
set -eu
IIRP_PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$IIRP_PROJECT_DIR"
[ "$(uname -s)" = Darwin ] || { echo '此脚本仅用于 Mac；Linux 请参阅 deploy/README.zh-CN.md。'; exit 1; }
command -v node >/dev/null || { echo '需要 Node.js 24 LTS。'; exit 1; }
command -v brew >/dev/null || { echo '需要已安装的 Homebrew。'; exit 1; }
if [ ! -x /opt/homebrew/opt/postgresql@18/bin/pg_ctl ] && [ ! -x /usr/local/opt/postgresql@18/bin/pg_ctl ]; then
  HOMEBREW_NO_INSTALL_CLEANUP=1 brew install postgresql@18
fi
export IIRP_PG_BIN="$(brew --prefix postgresql@18)/bin"
mkdir -p .tools
if [ ! -x .tools/bootstrap/bin/uv ]; then
  python3 -m venv .tools/bootstrap
  .tools/bootstrap/bin/pip install uv==0.12.10
fi
export UV_PYTHON_INSTALL_DIR="$IIRP_PROJECT_DIR/.tools/python"
export UV_PYTHON_BIN_DIR="$IIRP_PROJECT_DIR/.tools/bin"
export UV_LINK_MODE=copy
.tools/bootstrap/bin/uv sync --frozen --python 3.13
# macOS can inherit Finder hidden flags for .pth files during installation.
find .venv -name '*.pth' -exec chflags nohidden {} +
(cd frontend && npm ci && npm run build)
./iirp init
.venv/bin/python -m alembic upgrade head
printf '%s\n' '基础工程已安装。运行 ./iirp start 后访问 http://127.0.0.1:18081。'
