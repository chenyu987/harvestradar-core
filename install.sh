#!/bin/zsh
# Set up the ChatApp collector on this Mac, as a background service that starts
# at login and restarts if it crashes. Safe to run again (e.g. after git pull).
#
#   ./install.sh             install or update
#   ./install.sh status      is it running? last log lines
#   ./install.sh uninstall   remove the service (keeps data/ and collector.toml)
set -euo pipefail

REPO=${0:A:h}
LABEL=com.example.harvestradar
PLIST=~/Library/LaunchAgents/$LABEL.plist
DOMAIN=gui/$(id -u)
OUT=$REPO/data/collector

cmd=${1:-install}

if [[ $cmd == uninstall ]]; then
  launchctl bootout $DOMAIN/$LABEL 2>/dev/null || true
  rm -f $PLIST
  echo "已移除后台服务。data/ 和 collector.toml 都还在。"
  exit 0
fi

if [[ $cmd == status ]]; then
  launchctl print $DOMAIN/$LABEL 2>/dev/null | grep -E "state =|last exit code|pid =" || echo "后台服务没装。"
  [[ -f $OUT/STOP ]] && echo "STOP 文件在: $(cat $OUT/STOP)"
  [[ -f $OUT/log.jsonl ]] && { echo "--- 最近的日志"; tail -n 8 $OUT/log.jsonl; }
  exit 0
fi

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
PY=$REPO/.venv/bin/python
REAL=$($PY -c 'import os, sys; print(os.path.realpath(sys.executable))')

# 3. config
if [[ ! -f $REPO/collector.toml ]]; then
  cp $REPO/collector.example.toml $REPO/collector.toml
  echo "已建 collector.toml。把 groups 改成要看的群名（和聊天软件里显示的一样），再重跑 ./install.sh"
  open -e $REPO/collector.toml
  exit 0
fi
mkdir -p $OUT

# 4. background service
mkdir -p ~/Library/LaunchAgents
cat > $PLIST <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string><string>-m</string><string>harvestradar.collector</string><string>run</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <!-- restart after a crash or a missing permission, not after STOP or a kill switch -->
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>300</integer>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>StandardOutPath</key><string>$OUT/service.log</string>
  <key>StandardErrorPath</key><string>$OUT/service.log</string>
</dict>
</plist>
PL
launchctl bootout $DOMAIN/$LABEL 2>/dev/null || true
launchctl bootstrap $DOMAIN $PLIST

cat <<MSG

装好了，后台服务已启动，以后开机登录会自己跑。

还差一次性的两个权限（每台 Mac 只做一次）：
  系统设置 → 隐私与安全性 → 「屏幕录制」和「辅助功能」
  把列表里的 Python（$REAL）打开。
  列表里没有的话，点 + 按 ⌘⇧G 粘贴上面的路径加进去。
服务没权限时会弹通知提醒你，打开后 5 分钟内自己重试。

查看状态:  ./install.sh status
急停:      $PY -m harvestradar.collector stop
恢复:      $PY -m harvestradar.collector resume
MSG
