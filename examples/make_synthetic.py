#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成一份**合成**的 DeepSeek 导出样例，用于演示与测试。

数据全部是编造的，不含任何真实用户信息。它刻意覆盖了几个容易出错的形状：
  - 正常的 REQUEST / THINK / RESPONSE 节点
  - FILE 片段（只有元数据，没有字节）
  - FILE + REQUEST 同节点（随提问提交的附件）
  - SEARCH 片段（带 results 数组）
  - TOOL_OPEN / TOOL_FIND（只有 type，无载荷）
  - 分支（一个节点有多个 children）
  - 超长文本块（用于验证切块，避免被导入器裁剪）

用法：
    python examples/make_synthetic.py            # 写到 examples/synthetic_conversations.json
    python examples/make_synthetic.py --out x.json
"""

from __future__ import annotations

import argparse
import json
import os

LONG_PARAGRAPH = (
    "这是一段刻意写得很长的合成文本，用来验证转换工具会不会把超长块切开。"
    "如果不切开，dsh-chat-import 的第一层保护会把这段文本砍成「头 75% + 尾 25%」，"
    "中间的部分在落盘的会话日志里就永久消失了。"
) * 220  # 约 24,000 字符，刻意超过 16,000 上限以验证切块逻辑


def message(model: str, inserted_at: str, fragments: list[dict]) -> dict:
    return {"model": model, "inserted_at": inserted_at, "fragments": fragments}


def build() -> list[dict]:
    return [
        {
            "id": "00000000-0000-4000-8000-000000000001",
            "title": "示例：一次普通对话",
            "inserted_at": "2026-01-02T09:00:00.000000+08:00",
            "updated_at": "2026-01-02T09:20:00.000000+08:00",
            "mapping": {
                "root": {"id": "root", "parent": None, "children": ["1"], "message": None},
                "1": {
                    "id": "1", "parent": "root", "children": ["2"],
                    "message": message("deepseek-reasoner", "2026-01-02T09:00:00.877000+08:00", [
                        {"type": "REQUEST", "content": "帮我把这句话改写得更简洁。"},
                    ]),
                },
                "2": {
                    "id": "2", "parent": "1", "children": ["3"],
                    "message": message("deepseek-reasoner", "2026-01-02T09:00:05.000000+08:00", [
                        {"type": "THINK", "content": "用户要的是精简，我先删掉冗余修饰词。"},
                        {"type": "RESPONSE", "content": "改好了：**把这句话写短一点。**"},
                    ]),
                },
                "3": {
                    "id": "3", "parent": "2", "children": ["4"],
                    "message": message("deepseek-reasoner", "2026-01-02T09:10:00.000000+08:00", [
                        {"type": "REQUEST", "content": "顺便看看这张截图里的数据对不对。"},
                        {"type": "FILE", "files": [
                            {"file_id": "ffffffff-0000-4000-8000-00000000000a",
                             "file_name": "synthetic_screenshot.png", "file_size": 123456},
                        ]},
                    ]),
                },
                "4": {
                    "id": "4", "parent": "3", "children": [],
                    "message": message("deepseek-chat", "2026-01-02T09:10:20.000000+08:00", [
                        {"type": "THINK", "content": "我先联网核对一下这个口径。"},
                        {"type": "TOOL_OPEN"},
                        {"type": "TOOL_SEARCH", "results": [
                            {"url": "https://example.com/a", "title": "合成结果一"},
                            {"url": "https://example.com/b", "title": "合成结果二"},
                        ]},
                        {"type": "RESPONSE", "content": "联网核对过了，数据口径没问题。"},
                    ]),
                },
            },
        },
        {
            "id": "00000000-0000-4000-8000-000000000002",
            "title": "示例：分支 + 无载荷片段 + 超长块",
            "inserted_at": "2026-03-04T21:30:00.000000+08:00",
            "updated_at": "2026-03-04T22:00:00.000000+08:00",
            "mapping": {
                "root": {"id": "root", "parent": None, "children": ["1"], "message": None},
                "1": {
                    "id": "1", "parent": "root", "children": ["2"],
                    "message": message("deepseek-reasoner", "2026-03-04T21:30:00.000000+08:00", [
                        {"type": "REQUEST", "content": "给我一段很长的说明。"},
                    ]),
                },
                "2": {
                    "id": "2", "parent": "1", "children": ["3a", "3b"],
                    "message": message("deepseek-reasoner", "2026-03-04T21:30:03.000000+08:00", [
                        {"type": "THINK", "content": "这里会产生分支：用户重新生成了一次回答。"},
                        {"type": "RESPONSE", "content": "下面是第一版回答。"},
                    ]),
                },
                # 主链会走 3a（子树更大）
                "3a": {
                    "id": "3a", "parent": "2", "children": ["4"],
                    "message": message("deepseek-reasoner", "2026-03-04T21:31:00.000000+08:00", [
                        {"type": "TOOL_FIND"},
                        {"type": "RESPONSE", "content": LONG_PARAGRAPH},
                    ]),
                },
                # 分支：被重新生成掉的回答，落在主链外
                "3b": {
                    "id": "3b", "parent": "2", "children": [],
                    "message": message("deepseek-reasoner", "2026-03-04T21:31:00.000000+08:00", [
                        {"type": "RESPONSE", "content": "这是被重新生成掉的旧回答（应出现在报告里）。"},
                    ]),
                },
                "4": {
                    "id": "4", "parent": "3a", "children": ["5"],
                    "message": message("deepseek-reasoner", "2026-03-04T21:32:00.000000+08:00", [
                        {"type": "FILE", "files": [
                            {"file_id": "ffffffff-0000-4000-8000-00000000000b",
                             "file_name": "standalone_attachment.jpg", "file_size": 234567},
                        ]},
                    ]),
                },
                "5": {
                    "id": "5", "parent": "4", "children": [],
                    "message": message("deepseek-reasoner", "2026-03-04T21:33:00.000000+08:00", [
                        {"type": "SEARCH", "results": [
                            {"url": "https://example.com/c", "title": "合成结果三"},
                        ]},
                        {"type": "RESPONSE", "content": "结束。"},
                    ]),
                },
            },
        },
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="生成合成的 DeepSeek 导出样例")
    parser.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                     "synthetic_conversations.json"))
    args = parser.parse_args()
    data = build()
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    print(f"[ok] 已写出 {len(data)} 个合成会话 -> {args.out}")
    print("[note] 全部为编造数据，不含任何真实用户信息。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
