---
name: auto-organizer
version: 1.3.0
description: Use when the user asks to organize, classify, sort, or archive a local folder. 当用户说“帮我整理这个文件夹”“把这些文件归类”“整理桌面/下载目录”“归档资料”，或要求对某个目录做分类、归类、整理时使用。不适用于零散单文件、只改名、或未提供路径的情况。
---

# Auto-Organizer（自动文档整理）

把一个杂乱的本地文件夹按主题归类。机械操作交给脚本，语义判断由你完成。

> 下文 `scripts/...` 均相对于本 SKILL.md 所在目录。命令统一用 `python`。
> 如果当前工作目录不是本 skill 目录，执行前先把脚本路径替换为绝对路径。
>
> **只使用本文件、`references/`、`scripts/`。** 不要读取 `README.md`、`tests/`、`.git/`，它们不是工作流的一部分。

## 适用 / 不适用

**适用**：用户提供**一个本地文件夹路径**，目标是按主题归类整理。
**不适用**：零散单文件、无统一主题；只要求重命名；用户未提供路径（先问，不要猜）。

## 安全红线（必须遵守）

1. **先把完整文件树发给用户，再等确认**：必须列出每个将移动的文件、目标分类、判定依据、未识别清单。用户必须在**看到这棵树之后**明确说「确认」「可以执行」。同一轮里用户还没看到树就说「直接整理 / 不用问了」，那不是确认，禁止 `--apply`。
2. **回滚同样先预览**：`--journal latest` 只还原最近一次 apply。先 dry-run 列出「新路径 → 原路径」，确认后再加 `--apply`。
3. **只在根目录内部移动**：不跨盘、不移动根目录外的文件、不删除文件。搬空的源目录可以删，预先就空的目录保留。
4. **绝不覆盖**：目标重名时脚本自动追加 `(2)`、`(3)`。
5. 只处理用户指定的根目录，不顺手整理其父目录或桌面。

## 前置检查

1. 根目录存在且是文件夹；向用户复述绝对路径。
2. Python 可用：`python --version`。不可用时改用 Read/Glob/Bash 手工完成，并在回复中说明降级。
3. 目录可写：无法写入时停止并告知。

## 执行流程

### 1. 扫描

```bash
python scripts/organize.py scan "<根目录>" --output "<根目录>/.organize/scan.json"
```

- 读取 `scan.json` 的 `files` / `skipped` / `empty_dirs` / `duplicate_groups` / `stats`。
- 脚本自动跳过 `.organize/`、`.git/`、`_整理报告.md`、点文件、隐藏/系统文件、符号链接；需要处理隐藏文件时加 `--include-hidden`（`.git` 和 `.env` 仍跳过）。
- `scan.json` 里 `already_categorized=true` 表示文件已在本 skill 生成的分类目录（`1 活动策划` 这种 1–2 位编号）中。默认放入 `unmatched`，除非用户要求重新整理。`2024 年报` 这种年份目录不算已整理。
- 每个文件带 `sha256`。`duplicate_groups` 列出内容完全相同的组，并给出建议保留的 `keep`（优先不带 `(2)` 的文件名）和 `extras`。
- 扫描不会修改任何文件。

### 2. 判定（仅对文件名判断不了的）

- 先看文件名、扩展名、所在子目录、修改时间。
- 仍含糊时抽取正文：

```bash
python scripts/extract_text.py "<文件1>" "<文件2>" --max-chars 2000
```

- 输出 JSON，每条含 `type` / `text` / `meta` / `notes`。
- 支持 docx / pptx / xlsx / pdf / 纯文本 / 图片 EXIF / zip 清单；核心只用标准库，PDF 若已安装 `pypdf`/`PyPDF2` 会自动用。
- 图片：本机 PATH 中有 Tesseract OCR 时识别文字（优先 `chi_sim+eng`）。没有则 notes 写明「未 OCR：本机未安装或未找到 Tesseract OCR」，按文件名/同批判断。扫描件 PDF 不会交给 Tesseract（它只处理图片），notes 同样写原因。
- 抽取失败、扫描件或无正文线索时，按文件名/同批文件/目录上下文判断，并在报告中注明。

### 3. 规划分类

- 通读全部文件后按**主题/环节**细分，不要按扩展名分，也不要预设与当前文件无关的类别。
- 主题不同就单独立类。策划、场地、经费、宣传、照片、总结可以同时存在；**不要**用「活动筹备」把策划+场地+预算装进一个口袋。
- 某类只有 1–2 个文件也可以保留，只要主题和其他类明显不同。简历、合同等个人材料单独成类，不要因为份数少就丢进 `其他资料`。
- 不要为凑数量发明无意义的类；也不要为了压类数而合并不同环节。
- 有真正杂项时，最后一类才是 `其他资料`；没有杂项就不要空留这一类。
- **内容重复（SHA-256 相同）**：`keep` 那一份进主题分类；`extras` 全部移入 `N 重复文件`，reason 写「与 keep 内容相同」，**不删除**。预览树里必须列出每一组重复。没有重复就不要建「重复文件」类。
- 编号从 1 连续递增，不跳号；名称 2–8 字，不含 `\ / : * ? " < > |`。
- 详细判定规则见 [references/分类规则.md](references/分类规则.md)。
- 生成 `plan.json`（UTF-8），写到 `<根目录>/.organize/plan.json`：

```json
{
  "root": "C:\\Users\\me\\Downloads\\待整理",
  "categories": [
    {"folder": "1 活动策划", "description": "策划案、流程、方案"},
    {"folder": "2 场地申请", "description": "申请表、审批、场地确认"},
    {"folder": "3 其他资料", "description": "无法归入以上类别"}
  ],
  "moves": [
    {"path": "女生节策划案.docx", "target": "1 活动策划", "reason": "文件名含“策划”"},
    {"path": "场地租借申请表.pdf", "target": "2 场地申请", "reason": "正文含“场地租借”"}
  ],
  "unmatched": [
    {"path": "IMG_9999.bin", "reason": "无扩展名、无正文线索"}
  ]
}
```

- 每个扫描到的文件要么进 `moves`，要么进 `unmatched`，不要遗漏。
- `path` 用 `scan.json` 中的相对路径（`/` 分隔）；`target` 必须与 `categories[].folder` 完全一致。

### 4. 预览并等待确认

```bash
python scripts/organize.py apply "<根目录>" --plan "<根目录>/.organize/plan.json"
```

不带 `--apply` 是 dry-run：只打印结构树与判定依据，不改动任何文件。
把结果整理成下面的格式回复用户。**发完这棵树之后停下，等待用户明确确认；本轮不得加 `--apply`。**

```text
待整理目录：C:\Users\me\Downloads\待整理
├── 1 活动策划（3 个）
│   ├── 女生节策划案.docx — 文件名含“策划”
│   └── ...
├── 2 场地申请（2 个）
│   └── ...
└── 3 其他资料（1 个）
├── 4 重复文件（2 个）
│   └── 讲义 (1).pdf — 与 讲义.pdf 内容相同（SHA-256）
未识别：2 个（保留原位）
预计移动 18 个文件，跳过 3 个。确认后执行。
嵌套文件会拍平到分类文件夹根下；搬空的源目录会被删除，预先就空的目录保留。
内容相同的副本只改存放位置，不删除。
```

### 5. 执行

用户确认后：

```bash
python scripts/organize.py apply "<根目录>" --plan "<根目录>/.organize/plan.json" --apply
```

- 输出成功 / 跳过 / 失败数量。
- 生成 `<根目录>/_整理报告.md` 与 `.organize/journal-*.json`。
- 搬空的源目录会被删除；预先就空的目录保留。
- 有失败项时退出码为 2；逐条说明原因，不要假装成功。
- 若存在 `scan.json`，每个扫描到的文件必须出现在 `moves` 或 `unmatched`，否则 apply（含 dry-run）会失败。

### 6. 校验与交付

```bash
python scripts/organize.py verify "<根目录>" --journal latest
```

- 确认所有已移动文件都在目标位置。
- 把 `_整理报告.md` 作为交付物发送给用户，并说明失败/未识别清单。
- 告知回滚命令。

## 回滚

```bash
python scripts/organize.py rollback "<根目录>" --journal latest          # dry-run
python scripts/organize.py rollback "<根目录>" --journal latest --apply  # 真正回滚
```

- `--journal latest` **只还原最近一次** apply，不是全部历史。要还原更早一次，把 journal 文件名发给脚本。
- 先 dry-run，把「新路径 → 原路径」清单发给用户，确认后再 `--apply`。
- 按 journal 逆序移回；原位置已有文件时不覆盖，记为冲突。
- 搬空后删除的源目录会按原路径重建。

## 输出格式

- **确认阶段**：结构树 + 文件数 + 每个文件的判定依据 + 未识别清单。
- **完成后**：`_整理报告.md`（原路径 → 新路径、判定依据、失败/未识别清单、回滚命令）。

## 异常处理

- **文件被占用 / 无权限**：脚本记录为 failed，继续处理其余文件；报告中列出。
- **预先就空的文件夹**：记录在 `empty_dirs`，保持原样。
- **搬空的源目录**：apply 后删除；回滚时会按原路径重建。
- **已分类文件**：`already_categorized=true` 时默认不二次移动。
- **重名**：自动追加 `(2)`、`(3)`，绝不覆盖。
- **隐藏/系统文件、符号链接/目录联接**：默认跳过，列入 `skipped`。
- **路径过长**：脚本内部自动加 `\\?\` 前缀。
- **旧版 `.doc/.xls/.ppt`、扫描版 PDF、HEIC**：按文件名/目录判断，报告中注明未抽取正文。
- **Python 不可用**：用内置工具手工完成同样的“预览 → 确认 → 移动 → 报告”流程。

## 常见错误

- 把「赶紧整理 / 不用问了」当成对**当前方案**的确认。没看到结构树之前，不能 `--apply`。
- 按扩展名分成「文档 / 图片 / 其他」，等于没整理。
- 把策划、场地、预算合成「活动筹备」——主题不同，不要装进一个口袋。
- 为了压成 2–4 类而合并不同环节；或按扩展名分成「文档 / 图片 / 其他」。
- 漏文件：没进 `moves` 也没进 `unmatched`。
- 发现 SHA-256 相同却删除副本，或把 keep 和 extras 都丢进主题类还不标明。
- 对已经整理过的编号目录再搬一次，除非用户明确要求重新分类。
- 去读 `README.md` 或运行 `tests/`。整理流程只跟本文件、`references/`、`scripts/` 有关。

## 资源

- `scripts/organize.py`：scan / apply（默认 dry-run）/ verify / rollback / 报告 / journal
- `scripts/extract_text.py`：docx / pptx / xlsx / pdf / 文本 / 图片（可选 Tesseract OCR）/ zip 清单抽取
- `references/分类规则.md`：判定优先级、类别设计、边界情况
- 不要打开 `README.md` 或 `tests/`
