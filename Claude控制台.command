#!/bin/bash
# 双击启动 Claude Code 控制台（图形面板）
# 已在运行就只把浏览器打开

PORT="${CCP_PORT:-7788}"
PANEL="$(cd "$(dirname "$0")" && pwd)"
cd "$PANEL" || exit 1

if curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/"; then
  open "http://127.0.0.1:$PORT/"
  echo "控制台已在运行：http://127.0.0.1:$PORT/"
  exit 0
fi

echo "启动控制台…（$PANEL）"
nohup python3 "$PANEL/console.py" > /tmp/ccp.log 2>&1 &

for i in $(seq 1 30); do
  sleep 0.4
  if curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/"; then
    open "http://127.0.0.1:$PORT/"
    echo "已打开：http://127.0.0.1:$PORT/"
    exit 0
  fi
done

echo "启动失败，日志：/tmp/ccp.log"
cat /tmp/ccp.log
exit 1
