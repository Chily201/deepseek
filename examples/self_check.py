#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自检脚本：用合成样例跑一遍转换，并断言关键行为。

不依赖 pytest，直接 python examples/self_check.py 即可。
任何一条断言失败都会以非零状态退出。

覆盖的断言：
  1. 输出文件数与源会话数一致
  2. 每个文件最前面就是 interchange 标记（探测只扫文件头）
  3. 所有文本块都 <= 上限（否则导入器会裁掉中间内容）
  4. 每个 toolResult 的 toolCallId 都能在某个 toolCall 里找到配对
  5. 主链外的分支节点出现在报告里，而不是静默消失
  6. 只有 type 字段的片段（TOOL_OPEN / TOOL_FIND）被计数
  7. 无字节的 FILE 片段登记进报告，不伪造图片块
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SAMPLE = os.path.join(HERE, "synthetic_conversations.json")
SCRIPT = os.path.join(ROOT, "deepseek_to_interchange.py")
TEXT_LIMIT = 16000

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        failures.append(label)


def main() -> int:
    if not os.path.isfile(SAMPLE):
        print("[setup] 合成样例不存在，先生成…")
        subprocess.run([sys.executable, os.path.join(HERE, "make_synthetic.py")], check=True)

    workdir = tempfile.mkdtemp(prefix="dsh-convert-selfcheck-")
    outdir = os.path.join(workdir, "out")
    report = os.path.join(workdir, "report.json")
    try:
        subprocess.run(
            [sys.executable, SCRIPT, SAMPLE, "--out", outdir, "--report", report, "--quiet"],
            check=True, cwd=ROOT,
        )

        with open(SAMPLE, encoding="utf-8") as handle:
            source = json.load(handle)
        with open(report, encoding="utf-8") as handle:
            rep = json.load(handle)

        files = sorted(f for f in os.listdir(outdir) if f.endswith(".interchange.jsonl"))
        print(f"\n[1] 输出文件数 = {len(files)}（源会话 {len(source)}）")
        check(len(files) == len(source), "输出文件数与源会话数一致")

        docs = []
        for name in files:
            with open(os.path.join(outdir, name), encoding="utf-8") as handle:
                docs.append(json.load(handle))

        print("\n[2] 标记位置")
        for name, doc in zip(files, docs):
            check(doc.get("interchange") == "dsh-chat-import" and doc.get("version") == 1,
                  f"{name} 含 interchange 标记")
        # 文件最开头就是标记（序列化时 interchange 是第一个键）
        with open(os.path.join(outdir, files[0]), encoding="utf-8") as handle:
            head = handle.read(200)
        check(head.lstrip().startswith('{"interchange"'), "标记位于文件最前面")

        print("\n[3] 文本块长度上限")
        over = [(name, len(b["text"]))
                for name, doc in zip(files, docs)
                for turn in doc["turns"] for step in turn.get("steps", [])
                for b in step.get("content", []) if len(b.get("text", "")) > TEXT_LIMIT]
        check(not over, f"没有任何文本块超过 {TEXT_LIMIT} 字符（超出：{over}）")

        print("\n[4] 工具调用与结果配对")
        unpaired = []
        for name, doc in zip(files, docs):
            call_ids = {c["id"] for turn in doc["turns"] for step in turn.get("steps", [])
                        for c in step.get("toolCalls", [])}
            for turn in doc["turns"]:
                for step in turn.get("steps", []):
                    for result in step.get("toolResults", []):
                        if result["toolCallId"] not in call_ids:
                            unpaired.append((name, result["toolCallId"]))
        check(not unpaired, f"所有 toolResult 都有配对调用（孤立：{unpaired}）")

        print("\n[5] 主链外的分支节点被记录")
        dropped = rep.get("dropped_off_main_path_nodes", [])
        check(len(dropped) >= 1, f"报告记录了主链外节点（{len(dropped)} 个）")
        check(any("重新生成" in (d.get("preview") or "") for d in dropped),
              "被丢弃的分支回答出现在报告预览里")

        print("\n[6] 无载荷片段被计数")
        counts = rep.get("counts", {})
        check(counts.get("tool_open_dropped", 0) >= 1, "TOOL_OPEN 被计数")
        check(counts.get("tool_find_dropped", 0) >= 1, "TOOL_FIND 被计数")

        print("\n[7] 无字节附件只登记、不伪造")
        manifest = rep.get("file_only_attachments", [])
        check(len(manifest) >= 1, f"无字节附件登记进报告（{len(manifest)} 个）")
        image_blocks = [b for doc in docs for turn in doc["turns"]
                        for step in turn.get("steps", []) for b in step.get("content", [])
                        if b.get("type") == "image"]
        check(not image_blocks, "没有伪造任何 image 块")

        print("\n[8] 联网搜索结果被保留为工具结果")
        check(counts.get("search_results_kept", 0) >= 1, "搜索结果保留为 toolResults")

    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    if failures:
        print(f"✗ {len(failures)} 项未通过：")
        for item in failures:
            print(f"   - {item}")
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
