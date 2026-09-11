#!/bin/zsh
set -u

SCRIPT_DIR="${0:A:h}"
PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
  print -r -- "FIS 启动失败：未找到项目运行环境 .venv/bin/python。"
  print -r -- "请先完成开发环境配置，再重新双击此文件。"
  if [[ -t 0 ]]; then
    print -n -- "按回车键关闭此窗口…"
    read -r
  fi
  exit 1
fi

cd "$SCRIPT_DIR" || exit 1
export PYTHONPATH="$SCRIPT_DIR/src"
exec "$PYTHON_BIN" -m fis
