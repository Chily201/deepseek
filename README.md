# deepseek-export-to-dsh

把 **DeepSeek 官方数据导出**（`deepseek_data-*.zip` 里的 `conversations.json`）转换成
**[dsh-chat-import](https://github.com/Nwflower/dsh-chat-import) 的 interchange v1 文档**，
从而导入 **DeepSeek Harness (DSH)**，变成可打开、可续聊的会话。

零第三方依赖，只用 Python 标准库。不需要登录 DeepSeek，不调用任何私有接口——
只读你已经下载到本地的官方导出文件。

---

## 为什么需要这个东西

`dsh-chat-import` 内置了 **40 个来源解析器**：Claude Code、ChatGPT、Codex、Kimi、Qoder、
opencode、Cursor、Gemini……**其中没有 DeepSeek 官方导出的原生解析器。**

官方支持的路径是 **interchange v1**：`dsh-chat-import` 从 0.24.1 起把 interchange v1
当作一等导入格式，并注册了 `dsh-chat-import-convert` skill，让 Agent 按
[docs/INTERCHANGE-GUIDE.md](https://github.com/Nwflower/dsh-chat-import/blob/main/docs/INTERCHANGE-GUIDE.md)
把任意来源转成 interchange 文档。

所以缺的不是"路"，而是**一个现成的、可验证的转换器**——你没必要让 Agent 从零再写一遍。
本工具就是这样一个转换器，并且额外处理了两个实践中的坑（见下方「踩过的坑」）。

---

## 它做什么

1. 读 `conversations.json`（一个数组，每个元素是一个会话）
2. 每个会话产出一个 **interchange v1 文档**（v1 规范见 dsh-chat-import 的 `docs/INTERCHANGE.md`）
3. 同时产出一份**转换报告**，逐项说明哪些内容没能导入、为什么

## 它不做什么

- ❌ 不登录 DeepSeek、不碰私有接口
- ❌ 不修改你的源文件
- ❌ 不联网
- ❌ 不猜内容：源里没有的（token 用量、工具结果、时间戳）一律不编造

---

## 环境要求

**Python 3.11 或更高**（用到 `datetime.fromisoformat` 的带时区解析）。
**没有任何第三方依赖**，不需要 pip install。

```bash
python --version   # 确认 >= 3.11
```

---

## 先跑样例验证（推荐第一次这样做）

仓库自带一份**合成**样例数据（全部编造，不含任何真实用户信息），
覆盖了几个容易出错的形状：分支、无载荷片段、无字节附件、超长文本块。

```bash
python examples/make_synthetic.py     # 生成 examples/synthetic_conversations.json
python examples/self_check.py         # 转换 + 8 项断言
```

`self_check.py` 会检查：标记位置、文本块上限、工具调用与结果配对、
分支节点是否被记录、无载荷片段是否被计数、有没有伪造图片块等。全部通过会打印 `✓ 全部通过`。

---

## 用法

```bash
# 基本用法：转换成 interchange/ 目录
python deepseek_to_interchange.py conversations.json --out interchange/

# 不切分超长块（不推荐，原因见下方「坑 2」）
python deepseek_to_interchange.py conversations.json --out out/ --no-split-long-blocks

# 输出 .json 扩展名（只在你要逐个文件手动导入时才有意义）
python deepseek_to_interchange.py conversations.json --out out/ --ext json
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `source` | 必填 | `conversations.json` 的路径 |
| `--out` | `interchange` | 输出目录 |
| `--ext` | `jsonl` | 输出扩展名，`jsonl` 或 `json` |
| `--text-limit` | `16000` | 单个文本块字符上限 |
| `--no-split-long-blocks` | 关闭 | 不切分超长块 |
| `--report` | `convert-report.json` | 报告输出路径 |
| `--quiet` | 关闭 | 不打印进度 |

---

## 导入 DSH

推荐**目录批量**：

```
import_chat({ format: "local-jsonl", path: "<--out 指向的目录>" })
```

也可以在「导入会话」面板里用「从文件导入」，逐个文件导入。

> **为什么默认输出 `.jsonl`？**
> `dsh-chat-import` 的目录收集器（`collectJsonlFiles`）**只匹配 `.jsonl`**；
> 只有面板的「从文件导入」才接受 `.json`。所以要用目录批量，文件名必须是 `.jsonl`。
> 文件内容仍然是一个完整的 JSON 文档，不是 JSONL。

导入后建议核对：会话数、标题、原始时间是否正确。

---

## 片段映射：DeepSeek 的什么东西去了哪里

DeepSeek 导出里每个消息节点带一个 `fragments` 数组。映射关系如下：

| DeepSeek 片段 | 去向 | 说明 |
| --- | --- | --- |
| `REQUEST` | 轮次的 `prompt` | 用户提问正文 |
| `THINK` | 内容块 `reasoning` | 推理过程，完整保留 |
| `RESPONSE` | 内容块 `text` | 助手可见正文 |
| `SEARCH` | `toolCalls` + `toolResults` | 渲染为名为 `web_search` 的工具调用与结果，`results` 里的标题和 URL 原样保留 |
| `TOOL_SEARCH` | 同上 | |
| `FILE`（与 `REQUEST` 同节点） | 追加到 `prompt` 的文本 | 形如 `[附件] xxx.png (791 KB)` |
| `FILE`（独立节点） | **只登记进报告** | 没有字节也没有 URL，不能伪造图片块 |
| `TOOL_OPEN` | **丢弃并计数** | 该片段只有 `type` 字段，没有任何载荷 |
| `TOOL_FIND` | **丢弃并计数** | 同上 |

时间戳、模型名逐节点保留；`meta.createdAt` 取自会话的 `inserted_at`。

---

## 踩过的坑（本文档最实用的部分）

这些是实际踩到并验证过的行为，写下来省你几个小时。

### 坑 1：目录批量只收 `.jsonl`

`dsh-chat-import` 的 `lib/import-batch.mjs` 里，`collectJsonlFiles` 的匹配是
`/\.jsonl$/i`。把 540 个 `.interchange.json` 放进目录再批量导入，结果是
**「共扫描 0 个文件」**。本工具因此默认输出 `.jsonl`。

### 坑 2：超长块会被静默裁掉（本工具的主要价值之一）

`dsh-chat-import` 的转换层有一层「预算保护」（`lib/convert/trim.mjs`），其中第一层是：

- 单条文本 / 推理 > **16,000 字符** → 裁成「头 75% + 尾 25%」，中间换成 `…（已裁剪）…`
- 单条工具结果 > **40,000 字符** → 同上

**关键在于：这一步发生在事件日志合成之前**（`finishSession` 里先 `applyBudgetTrim` 再
`synthesizeSession`）。也就是说，**被裁掉的是最终落盘的会话内容**，不只是模型上下文。

本工具默认**先把超长块按行边界切开**（每块 ≤ 上限），使裁剪条件不成立，内容一字不减。
切块只在相邻位置产生多个块，语义不变。

同时报告里会给出 `long_blocks_split` 计数，你可以确认到底切了几块。

### 坑 3：导入后的标题会带一个来源前缀

导入后的会话标题形如 `JSONL · <原标题>`。这个标签来自 `lib/sourced-title.mjs` 里按
**导入格式**硬编码的来源名（`local-jsonl` → `'Local JSONL'` → 归一为 `'JSONL'`），
**无法从 interchange 文档里控制**。属于观感问题，不影响内容。

### 坑 4：`FILE` 片段没有字节

`FILE` 片段形如：

```json
{"type": "FILE", "files": [{"file_id": "...", "file_name": "x.png", "file_size": 791571}]}
```

**只有元数据**，没有 URL、没有 base64。所以图片无法还原。
interchange 规范要求图片块必须带合法 `data`，否则会降级成 `[image]` 占位并计入降级数。
本工具的处理是：**与提问同节点的附件**写进 `prompt` 的文本；
**独立节点的附件**只登记进报告，不往正文里塞可能误导的内容。

### 坑 5：图会分叉

重新生成、编辑消息都会在 `mapping` 里产生多个 `children`。
本工具选**子树最大**的那条链作为主链（约等于网页端展示的线程），
**其余节点完整记录在报告的 `dropped_off_main_path_nodes` 里**（含类型和内容预览），
不会静默消失。

---

## 已知限制

- **只导主链。** 分叉出来的回答不会生成独立会话；内容在报告里，但不在会话里。
- **不导入 token 用量。** DeepSeek 官方导出里没有这个字段，不编造。
- **无字节附件不还原为图片。**
- **未做 CI / 多版本测试。** 目前只在 Python 3.11+ 的纯标准库环境下验证。

---

## 已验证范围

在一份 **540 个会话的官方导出（2026-10）** 上跑通过，计数如下（供你对照自己的结果）：

| 项 | 数量 |
| --- | --- |
| 源会话数 | 540 |
| 写出的 interchange 文档 | 540 |
| 轮次 turns | 2,272 |
| 步骤 steps | 2,722 |
| `THINK` 块 | 3,994 |
| `RESPONSE` 块 | 2,495 |
| `SEARCH` / `TOOL_SEARCH` 片段 | 1,608（其中 7 个 `results` 为空被丢弃） |
| 搜索结果保留为工具结果 | 1,601 |
| 随提问的附件（写进 prompt） | 96 |
| `TOOL_OPEN` 丢弃 | 4,486 |
| `TOOL_FIND` 丢弃 | 1 |
| 独立 `FILE` 节点 | 225（展开为 235 条附件记录） |
| 被切开的超长块 | 10 |
| 主链外未导出的节点 | 83 |

---

## 相关实现

同一个目标已经有别的实现，这里如实列出，方便你选：

- **[@knopki 的 `deepseek-to-interchange.mjs`](https://gist.github.com/knopki/ea43aa2a04171f1a9e3e3639c8744ed0)**
  （2026-10-04）：Node 单文件转换器。dsh-chat-import 作者已在 **0.25.2** 上用它做过
  DeepSeek 官方导出的端到端验证（见 [issue #70](https://github.com/Nwflower/dsh-chat-import/issues/70)）。

两者都能用，差异如下：

| | knopki 的 gist | 本项目 |
| --- | --- | --- |
| 运行时 | Node（`.mjs`，≥18） | Python（`>=3.11`），纯标准库 |
| **超长块切分** | 无 | **有**（避免被静默裁剪，见「坑 2」） |
| 输出扩展名 | `.interchange.json` | `.jsonl`（适配目录收集器，见「坑 1」） |
| 自检与样例 | 无 | 8 项断言 + 合成样例 |
| 分支选择 | 取 `inserted_at` 最新的子节点 | 取子树最大的链 |
| 过滤参数 | `--only` / `--from` / `--dry-run` | 无 |

选哪个取决于你的运行时，以及你要不要那两个坑的保护。

---

## 许可

MIT。见 [LICENSE](LICENSE)。

本项目与 DeepSeek、DeepSeek Harness、dsh-chat-import 均无隶属关系。
DeepSeek 是 DeepSeek 公司的商标。请只处理你自己有权访问的数据。

---

## English

Converts DeepSeek's official data export (`conversations.json` from `deepseek_data-*.zip`)
into [dsh-chat-import](https://github.com/Nwflower/dsh-chat-import) **interchange v1**
documents, so they can be imported into **DeepSeek Harness (DSH)** as resumable sessions.

Why: `dsh-chat-import` ships 40 source parsers but has no parser for DeepSeek's official
export. This fills that gap.

Standard library only (Python 3.11+), no network, no credentials, source files untouched.

```bash
python examples/make_synthetic.py && python examples/self_check.py   # verify
python deepseek_to_interchange.py conversations.json --out interchange/
import_chat({ format: "local-jsonl", path: "interchange/" })
```

Notes worth knowing: output files use the `.jsonl` extension because dsh-chat-import's
directory collector only globs `.jsonl`; text blocks longer than 16,000 characters are
split to avoid dsh-chat-import's silent block cropping (which happens *before* the session
log is written); `TOOL_OPEN`/`TOOL_FIND` fragments carry no payload and are dropped and
counted; `FILE` fragments carry metadata only, so images cannot be restored.

MIT licensed. Not affiliated with DeepSeek or dsh-chat-import.
