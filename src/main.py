# -*- coding: utf-8 -*-
"""PUBG-MapRuler 入口：DPI 感知 → 单实例 → 配置 → 数据 → 浮层主循环。

投影行为与取证结论见 overlay.py；模块划分见各文件 docstring。
"""
import json
import sys
import traceback

from winenv import enable_dpi_awareness, user32
from winhost import single_instance_guard
from appconfig import APP_TITLE, Config, resource, user_config_dir
from overlay import App


def main():
    enable_dpi_awareness()
    mutex = single_instance_guard()
    if mutex is None:
        return
    cfg = Config()
    data = json.loads(resource("data/pubg-overlay-data.json").read_text(encoding="utf-8"))
    App(cfg, data).run()
    _ = mutex


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log = user_config_dir() / "startup-error.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        user32.MessageBoxW(0, f"启动失败，详情见：\n{log}", APP_TITLE, 0x10)
        sys.exit(1)
