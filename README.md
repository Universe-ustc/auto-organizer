# Auto-Organizer

按主题整理本地文件夹的 Agent Skill。脚本负责扫描、移动、报告和回滚；分类方案由 Agent 根据文件名和正文来写。

这是标准的 Agent Skill：目录里有 `SKILL.md` 即可。Cursor、Claude Code、Codex、Copilot CLI、Gemini CLI 以及其它会加载技能目录的 Agent 都能用。

## 安装

把整个仓库（或克隆后的目录）复制到**当前 Agent 会扫描的技能目录**，并保持文件夹名为 `auto-organizer`：

```text
<skills>/auto-organizer/
  SKILL.md
  references/
  scripts/
```

常见个人技能目录（把 `<skills>` 换成其中一行即可）：

| Agent | 个人技能目录 |
| --- | --- |
| 跨工具共用（Codex / Copilot CLI / Gemini CLI 等） | `~/.agents/skills/` |
| Codex | `~/.codex/skills/` |
| Claude Code | `~/.claude/skills/` |
| Cursor | `~/.cursor/skills/` |

也可以放进当前项目，例如 `.agents/skills/auto-organizer/`、`.claude/skills/auto-organizer/`、`.cursor/skills/auto-organizer/`。仓库已经在工作区里时，直接在对话里 `@` 该目录即可，不必先复制。

Windows 示例（写入跨工具共用目录）：

```powershell
Copy-Item -Recurse ".\auto-organizer" "$env:USERPROFILE\.agents\skills\auto-organizer"
```

装好后，对 Agent 说「帮我整理这个文件夹」并给出路径即可。

## 使用

1. 给出**一个本地文件夹**的绝对路径。
2. Agent 先扫描、给出完整分类文件树，**你确认后**才真正移动文件。
3. 完成后查看目录里的 `_整理报告.md`。回滚前同样先预览；`latest` 只还原最近一次整理。

不适用于：只改名、零散单文件、没有给出路径。

## 安全

- 默认 dry-run，没有明确确认不会移动文件。
- 只在指定根目录内移动，不删除文件，不覆盖同名文件（自动加 `(2)`）。
- 跳过 `.git`、点文件、隐藏/系统文件和符号链接。
- 跳过 `.env`（即使要求包含隐藏文件也不动）。
- 每次执行写 `.organize/journal-*.json`，可用 `rollback` 还原。
- 内容完全相同的文件（SHA-256）会在预览里标出；确认后只把一份放进主题分类，其余移入「重复文件」，不删除。

## 依赖

- Python 3.8+（只要能运行 `python`）
- 标准库即可
- 可选：安装 `pypdf` 或 `PyPDF2` 后，PDF 正文抽取更稳
- 可选：系统 PATH 中有 [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) 时，给 jpg/png 等做文字识别。本机没有 Tesseract 时会跳过，并在 notes 里写明「未 OCR：本机未安装或未找到 Tesseract OCR」。扫描件 PDF 不会自动 OCR（Tesseract 只处理图片）。

## 测试

```powershell
python tests/test_auto_organizer.py -v
```

## 目录

```text
SKILL.md                 Agent 指令
references/分类规则.md   分类细则
scripts/organize.py      扫描 / 预览 / 执行 / 校验 / 回滚
scripts/extract_text.py  多格式正文抽取
tests/                   回归测试
```
