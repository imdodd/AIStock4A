"""交互式写入大模型 key，并立即校验

用法：
    python -m tools.set_llm_key

会提示粘贴 api_key（输入内容只写本地文件，不上传、不进命令历史），
写入 data/.secrets/llm.json 后自动跑一次连通性校验。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET = os.path.join(DATA_DIR, ".secrets", "llm.json")

DEFAULT = {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "",
    "model": "deepseek-chat",
    "temperature": 0.4,
}

PRESETS = {
    "1": ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
    "2": ("DeepSeek 推理版", "https://api.deepseek.com/v1", "deepseek-reasoner"),
    "3": ("通义千问（百炼兼容模式）", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
}


def mask(k: str) -> str:
    if not k:
        return "(未设置)"
    return k[:6] + "…" + k[-4:] if len(k) > 12 else "(已设置)"


def main() -> int:
    cfg = dict(DEFAULT)
    if os.path.exists(SECRET):
        try:
            cfg.update(json.load(open(SECRET, encoding="utf-8")))
        except Exception:
            print("! 现有 llm.json 解析失败，将按默认模板重建")

    print("=" * 58)
    print("大模型配置  ", SECRET)
    print("=" * 58)
    print(f"  接口地址 base_url : {cfg.get('base_url')}")
    print(f"  模型名   model    : {cfg.get('model')}")
    print(f"  密钥     api_key  : {mask(cfg.get('api_key') or '')}")
    print()
    print("服务商： 1) DeepSeek   2) DeepSeek 推理版   3) 通义千问   回车=保持当前")
    pick = input("选择 > ").strip()
    if pick in PRESETS:
        name, base, model = PRESETS[pick]
        cfg.update(base_url=base, model=model)
        print(f"  已切换为 {name} / {model}")
    print()
    key = input("粘贴 api_key（回车保留原值，输入 q 放弃）> ").strip()
    if key.lower() == "q":
        print("已取消，未改动任何内容。")
        return 0
    if key:
        cfg["api_key"] = key

    os.makedirs(os.path.dirname(SECRET), exist_ok=True)
    with open(SECRET, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(SECRET, 0o600)
    except Exception:
        pass
    print(f"\n已写入 {SECRET}（api_key = {mask(cfg.get('api_key') or '')}）\n")

    print("正在校验连通性……")
    r = subprocess.run([sys.executable, "-m", "tools.llm_check"], cwd=HERE)
    if r.returncode == 0:
        print("\n配置生效。刷新看板页面后，异动页追问区即为真流式对话，无需重启服务。")
    else:
        print("\n配置已保存但校验未通过，按上面的提示调整后重跑：python -m tools.set_llm_key")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
