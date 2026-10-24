#!/bin/zsh
# Set up the ChatApp collector on this Mac: Python, packages, collector.toml.
# Installs nothing that starts by itself; the collector runs only when you start it.
# Safe to run again (e.g. after git pull).
set -euo pipefail

REPO=${0:A:h}
OUT=$REPO/data/collector

[[ $(uname) == Darwin ]] || { echo "只能在 Mac 上装。"; exit 1; }

# 1. Python 3.11+ (tomllib). Homebrew's if there is one.
py=""
for c in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
         /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11 python3; do
  if command -v $c >/dev/null && $c -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    py=$c; break
  fi
done
if [[ -z $py ]]; then
  command -v brew >/dev/null || { echo "先装 Homebrew（https://brew.sh 那一行），再重跑 ./install.sh"; exit 1; }
  brew install python@3.12
  py=$(brew --prefix)/bin/python3.12
fi

# 2. venv + packages
if [[ ! -x $REPO/.venv/bin/python ]] || ! $REPO/.venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  rm -rf $REPO/.venv
  $py -m venv $REPO/.venv
fi
$REPO/.venv/bin/pip install -q --upgrade pip
$REPO/.venv/bin/pip install -q -r $REPO/requirements.txt

# 3. config
if [[ ! -f $REPO/collector.toml ]]; then
  cp $REPO/collector.example.toml $REPO/collector.toml
  echo "已建 collector.toml。把 groups 改成要看的群名（和聊天软件里显示的一样），再重跑 ./install.sh"
  open -e $REPO/collector.toml
  exit 0
fi
mkdir -p $OUT

cat <<MSG

装好了。它不会自己启动，要跑的时候在终端里：
  cd $REPO
  .venv/bin/python -m harvestradar.collector doctor              # 先查权限（不动鼠标键盘）
  .venv/bin/python -m harvestradar.collector run                 # 抓一遍，再把截图变成线索；不会重复跑
  .venv/bin/python -m harvestradar.collector stop                # 另开终端停掉

第一次要给「终端」开两个权限（每台 Mac 一次）：
  系统设置 → 隐私与安全性 → 「屏幕录制」和「辅助功能」→ 打开「终端」，然后重开终端。
MSG
