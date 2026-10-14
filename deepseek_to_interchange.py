#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deepseek_to_interchange.py

把 DeepSeek 官方数据导出（conversations.json）转换成 dsh-chat-import 的
interchange v1 文档，从而可以导入 DeepSeek Harness (DSH)。

为什么需要这个工具：
    dsh-chat-import 内置了 40 个来源解析器（Claude / ChatGPT / Codex / Kimi / ...），
    但没有 DeepSeek 官方导出的解析器。所以需要一个中间的格式转换。

零第三方依赖，只用 Python 标准库。需要 Python 3.11+（用到 datetime.fromisoformat 的
带时区解析）。

用法：
    python deepseek_to_interchange.py conversations.json --out interchange/
    python deepseek_to_interchange.py conversations.json --out out/ --no-split-long-blocks
    python deepseek_to_interchange.py conversations.json --out out/ --ext json

导出后如何导入 DSH：
    1) 目录批量（推荐）：
       import_chat({ format: "local-jsonl", path: "<out 目录>" })
       —— 注意 dsh-chat-import 的目录收集器只收 .jsonl，所以默认输出扩展名是 .jsonl
    2) 单文件：
       import_chat({ format: "local-jsonl", path: "<某个文件>" })
       或在「导入会话」面板用「从文件导入」

作者：见仓库 README ｜ 许可：MIT
"""

from __future__ import annotations

import argparse
import collections
import datetime as _dt
import json
import os
import re
import sys

# ── dsh-chat-import 转换层的硬编码上限（见其 lib/convert/trim.mjs）─────────────
# 它会把这些超限的块"头 75% + 尾 25%"裁掉，而且发生在写日志之前，
# 也就是被裁的是最终落盘的会话内容。本工具默认先把超长块按行边界切开，
# 使每块都在上限内，从而不触发裁剪、内容一字不减。
TEXT_BLOCK_CHAR_LIMIT = 16000
TOOL_RESULT_CHAR_LIMIT = 40000

# DeepSeek 导出里的片段类型：
#   REQUEST      用户提问正文
#   THINK        助手推理
#   RESPONSE     助手可见正文
#   FILE         附件元数据（file_id/file_name/file_size，无字节、无 URL）
#   SEARCH       联网搜索结果（results 数组）
#   TOOL_SEARCH  工具式搜索结果（results 数组）
#   TOOL_OPEN    仅 type 字段，无任何载荷 → 无法导入
#   TOOL_FIND    同上
REQUEST, THINK, RESPONSE = "REQUEST", "THINK", "RESPONSE"
SEARCH_TYPES = ("SEARCH", "TOOL_SEARCH")


# ── 时间 ─────────────────────────────────────────────────────────────────────
def to_epoch_ms(value: object) -> int | None:
    """把 DeepSeek 导出的 ISO 时间字符串转成 Unix 毫秒。拿不到就返回 None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return int(dt.timestamp() * 1000)


# ── 会话图遍历 ───────────────────────────────────────────────────────────────
def select_main_path(mapping: dict) -> list[str]:
    """
    从 "root" 出发选一条主链。

    DeepSeek 导出的 mapping 是一张有向图（节点含 id/parent/children），
    在重新生成或编辑时会产生分支。这里选"子树节点数最多"的那条链，
    等价于取最长线程——也就是网页端通常展示的那条。

    迭代实现 + 环保护，避免深链或异常数据导致递归溢出。
    """
    if not mapping or "root" not in mapping:
        return []

    # 后序迭代求每个节点的子树规模
    size: dict[str, int] = {}
    for start in mapping:
        if start in size:
            continue
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            node_id, done = stack.pop()
            if done:
                total = 1
                for child in mapping.get(node_id, {}).get("children") or []:
                    if child in mapping:
                        total += size.get(child, 1)
                size[node_id] = total
            else:
                if node_id in size:
                    continue
                stack.append((node_id, True))
                for child in mapping.get(node_id, {}).get("children") or []:
                    if child in mapping and child not in size:
                        stack.append((child, False))

    path: list[str] = []
    current = "root"
    for _ in range(len(mapping) + 1):  # 环保护
        path.append(current)
        children = [c for c in (mapping.get(current, {}).get("children") or []) if c in mapping]
        if not children:
            break
        current = max(children, key=lambda c: size.get(c, 1))
        if current in path:
            break
    return path


# ── 文本处理 ─────────────────────────────────────────────────────────────────
def split_long_text(text: str, limit: int) -> list[str]:
    """
    按行边界把超长文本切成多块，使每块 <= limit。

    目的：绕开 dsh-chat-import 的第一层裁剪（会把超限块砍掉中间部分）。
    切块不改变内容，只是把一段拆成相邻的多段。单行本身超限时硬切。
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buffer = ""
    for line in text.split("\n"):
        candidate = line if not buffer else buffer + "\n" + line
        if len(candidate) <= limit:
            buffer = candidate
            continue
        if buffer:
            chunks.append(buffer)
            buffer = ""
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        buffer = line
    if buffer:
        chunks.append(buffer)
    return chunks or [text]


def content_blocks(fragments: list[dict], text_limit: int, split_long: bool, stats: dict) -> list[dict]:
    """把 THINK / RESPONSE 片段转成 interchange 的 reasoning / text 内容块。"""
    blocks: list[dict] = []
    for frag in fragments:
        kind = frag.get("type")
        text = (frag.get("content") or "").strip()
        if not text:
            if kind in (THINK, RESPONSE):
                stats[f"empty_{kind.lower()}_skipped"] += 1
            continue
        if kind == THINK:
            block_type = "reasoning"
            stats["think_blocks"] += 1
        elif kind == RESPONSE:
            block_type = "text"
            stats["response_blocks"] += 1
        else:
            continue

        # 兼容工具结果上限：正文块用文本上限
        pieces = split_long_text(text, text_limit) if split_long else [text]
        if len(pieces) > 1:
            stats["long_blocks_split"] += 1
        for piece in pieces:
            blocks.append({"type": block_type, "text": piece})
    return blocks


def attachment_names(fragments: list[dict]) -> list[str]:
    """FILE 片段只有元数据（无字节、无 URL），这里取出文件名供正文标注。"""
    names: list[str] = []
    for frag in fragments:
        if frag.get("type") != "FILE":
            continue
        for item in frag.get("files") or []:
            name = item.get("file_name") or "(未命名)"
            size = item.get("file_size")
            if isinstance(size, int) and size > 0:
                name = f"{name} ({round(size / 1024)} KB)"
            names.append(name)
    return names


def search_result_text(frag: dict) -> str:
    """把搜索结果数组渲染成纯文本，作为工具结果保留。"""
    lines: list[str] = []
    for index, item in enumerate(frag.get("results") or [], 1):
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        if title and url:
            lines.append(f"{index}. {title}\n   {url}")
        elif url:
            lines.append(f"{index}. {url}")
        elif title:
            lines.append(f"{index}. {title}")
    return "\n".join(lines)


# ── 单个会话转换 ─────────────────────────────────────────────────────────────
def convert_conversation(conv: dict, options: argparse.Namespace, stats: dict) -> tuple[dict | None, list[dict]]:
    """返回 (interchange 文档 或 None, 主链外被丢弃的节点列表)。"""
    mapping = conv.get("mapping") or {}
    path = select_main_path(mapping)
    on_path = set(path)

    dropped: list[dict] = []
    for node_id, node in mapping.items():
        message = node.get("message")
        if isinstance(message, dict) and node_id not in on_path:
            dropped.append({
                "node": node_id,
                "types": sorted({f.get("type") for f in (message.get("fragments") or [])}),
                "preview": " | ".join(
                    (f.get("content") or "")[:120]
                    for f in (message.get("fragments") or []) if f.get("content")
                )[:200],
            })

    turns: list[dict] = []
    current_turn: dict | None = None
    models: collections.Counter = collections.Counter()
    file_only_nodes = 0

    for node_id in path:
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            continue
        message = node.get("message")
        if not isinstance(message, dict):
            continue

        fragments = message.get("fragments") or []
        types = [f.get("type") for f in fragments]
        time_ms = to_epoch_ms(message.get("inserted_at"))
        if message.get("model"):
            models[message["model"]] += 1

        # ── 用户提问节点 ──
        if REQUEST in types:
            prompt = "\n".join(
                (f.get("content") or "").strip()
                for f in fragments if f.get("type") == REQUEST and (f.get("content") or "").strip()
            ).strip()
            names = attachment_names(fragments)
            if names:
                note = "\n".join(f"[附件] {n}" for n in names)
                prompt = f"{prompt}\n{note}" if prompt else note
                stats["turns_with_inline_attachment"] += 1
            turn: dict = {"prompt": prompt, "steps": []}
            if time_ms:
                turn["time"] = time_ms
            turns.append(turn)
            current_turn = turn
            stats["turns"] += 1
            continue

        # ── 助手侧节点 ──
        blocks = content_blocks(fragments, options.text_limit, options.split_long_blocks, stats)

        tool_calls: list[dict] = []
        tool_results: list[dict] = []
        for index, frag in enumerate(fragments):
            kind = frag.get("type")
            if kind in SEARCH_TYPES:
                stats["search_fragments"] += 1
                results = frag.get("results") or []
                if results:
                    call_id = f"search-{node_id}-{index}"
                    tool_calls.append({"id": call_id, "name": "web_search", "arguments": "{}"})
                    tool_results.append({
                        "toolCallId": call_id,
                        "content": [{"type": "text", "text": search_result_text(frag)}],
                    })
                    stats["search_results_kept"] += 1
                else:
                    stats["search_fragments_empty_dropped"] += 1
            elif kind == "TOOL_OPEN":
                # 只有 type 字段，没有任何载荷，无处安放
                stats["tool_open_dropped"] += 1
            elif kind == "TOOL_FIND":
                stats["tool_find_dropped"] += 1
            elif kind == "FILE":
                # 无字节、无 URL → 不能伪造图片块；登记到清单而非正文
                file_only_nodes += 1
                for name in attachment_names([frag]):
                    stats.setdefault("_file_only_manifest", []).append({
                        "node": node_id, "attachment": name,
                    })

        step: dict = {}
        if blocks:
            step["content"] = blocks
        if tool_calls:
            step["toolCalls"] = tool_calls
        if tool_results:
            step["toolResults"] = tool_results
        if time_ms:
            step["time"] = time_ms
        if message.get("model"):
            step["model"] = message["model"]
        if not step:
            stats["empty_assistant_nodes_skipped"] += 1
            continue

        if current_turn is None:
            # 理论上不会发生（root 之后应当先出现 REQUEST），兜底成一个只有步骤的轮次
            current_turn = {"prompt": "", "steps": []}
            turns.append(current_turn)
            stats["turns_opened_without_request"] += 1
        current_turn["steps"].append(step)
        stats["steps"] += 1

    stats["file_only_nodes"] += file_only_nodes

    # 丢掉完全空的轮次（interchange 规范允许 prompt 为空，但整轮必须有内容）
    turns = [t for t in turns if (t.get("prompt") or "").strip() or t.get("steps")]
    if not turns:
        stats["conversations_rejected_empty"] += 1
        return None, dropped

    doc: dict = {
        # 这两个键必须出现在文件最前面：探测阶段只扫文件头
        "interchange": "dsh-chat-import",
        "version": 1,
        "meta": {
            "id": conv.get("id") or "deepseek-conversation",
            "createdAt": to_epoch_ms(conv.get("inserted_at")) or 0,
        },
        "title": conv.get("title") or "",
        "provider": "deepseek-web",
        "turns": turns,
    }
    if models:
        doc["model"] = models.most_common(1)[0][0]
    return doc, dropped


# ── 主流程 ───────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 DeepSeek 官方导出 conversations.json 转成 dsh-chat-import 的 interchange v1 文档。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "导入 DSH 示例：\n"
            '  import_chat({ format: "local-jsonl", path: "<out 目录>" })\n'
        ),
    )
    parser.add_argument("source", help="DeepSeek 导出的 conversations.json 路径")
    parser.add_argument("--out", default="interchange", help="输出目录（默认 interchange）")
    parser.add_argument("--ext", default="jsonl", choices=["jsonl", "json"],
                        help="输出扩展名（默认 jsonl：dsh-chat-import 的目录收集器只收 .jsonl）")
    parser.add_argument("--text-limit", type=int, default=TEXT_BLOCK_CHAR_LIMIT,
                        help=f"单个文本块的字符上限（默认 {TEXT_BLOCK_CHAR_LIMIT}）")
    parser.add_argument("--no-split-long-blocks", action="store_true",
                        help="不切分超长块（会触发 dsh-chat-import 的裁剪，丢掉中间内容）")
    parser.add_argument("--report", default="convert-report.json",
                        help="转换报告输出路径（默认 convert-report.json）")
    parser.add_argument("--quiet", action="store_true", help="不打印进度")
    options = parser.parse_args(argv)
    options.split_long_blocks = not options.no_split_long_blocks

    if not os.path.isfile(options.source):
        print(f"[error] 找不到源文件：{options.source}", file=sys.stderr)
        return 2

    try:
        with open(options.source, encoding="utf-8") as handle:
            conversations = json.load(handle)
    except json.JSONDecodeError as exc:
        print(f"[error] 源文件不是合法 JSON：{exc}", file=sys.stderr)
        return 2

    if isinstance(conversations, dict):
        # 容错：某些版本可能包一层
        conversations = conversations.get("conversations") or list(conversations.values())
    if not isinstance(conversations, list):
        print("[error] 源文件顶层应为数组", file=sys.stderr)
        return 2

    os.makedirs(options.out, exist_ok=True)

    stats: dict = collections.Counter()
    file_only_manifest: list[dict] = []
    dropped_nodes: list[dict] = []
    written = 0

    for index, conv in enumerate(conversations):
        if not isinstance(conv, dict):
            stats["non_object_conversations_skipped"] += 1
            continue
        doc, dropped = convert_conversation(conv, options, stats)
        for item in stats.pop("_file_only_manifest", []):
            item["conversation"] = doc.get("title") if doc else conv.get("title")
            file_only_manifest.append(item)
        for item in dropped:
            item["conversation"] = conv.get("title") or ""
            dropped_nodes.append(item)
        if doc is None:
            continue

        conv_id = re.sub(r"[^A-Za-z0-9._-]", "_", str(conv.get("id") or f"conv{index:04d}"))
        filename = f"{index:04d}-{conv_id}.interchange.{options.ext}"
        with open(os.path.join(options.out, filename), "w", encoding="utf-8") as handle:
            json.dump(doc, handle, ensure_ascii=False)
        written += 1

        if not options.quiet and (index + 1) % 100 == 0:
            print(f"  已处理 {index + 1}/{len(conversations)}，写出 {written}")

    report = {
        "source": os.path.abspath(options.source),
        "conversations_in_source": len(conversations),
        "files_written": written,
        "split_long_blocks": options.split_long_blocks,
        "text_block_char_limit": options.text_limit,
        "counts": dict(stats),
        "dropped_off_main_path_nodes": dropped_nodes,
        "file_only_attachments": file_only_manifest,
    }
    with open(options.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=1)

    print(json.dumps({k: v for k, v in report.items() if k != "dropped_off_main_path_nodes"}, ensure_ascii=False, indent=1))
    print(f"\n[done] 写出 {written} 个文档到 {os.path.abspath(options.out)}")
    print(f"[done] 报告：{os.path.abspath(options.report)}")
    if dropped_nodes:
        print(f"[note] 有 {len(dropped_nodes)} 个主链外的节点未导出（见报告的 dropped_off_main_path_nodes）")
    if file_only_manifest:
        print(f"[note] 有 {len(file_only_manifest)} 个无字节附件只登记在报告里（file_only_attachments）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
