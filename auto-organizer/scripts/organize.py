#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Auto-Organizer 机械执行器（仅标准库）。

职责边界：
- 本脚本只做扫描、校验、移动、报告、回滚等机械工作；
- 分类语义由调用方（Agent）写入 plan.json。

子命令：
  scan    扫描目录，输出文件清单 JSON
  apply   校验 plan.json；默认 dry-run，加 --apply 才移动
  verify  检查 journal 中已移动文件是否都在目标位置
  rollback 按 journal 逆序移回；默认 dry-run，加 --apply 才执行
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import stat as statmod
import sys
import time

INTERNAL_DIR = ".organize"
DEFAULT_REPORT = "_整理报告.md"
INVALID_NAME_CHARS = set('<>:"/\\|?*')
ALWAYS_SKIP_DIR_NAMES = {".git", ".svn", ".hg", INTERNAL_DIR}
# 仅匹配本 skill 生成的「1 活动策划」这类目录，避免把「2024 年报」当成已整理
CATEGORY_FOLDER_RE = re.compile(r"^\d{1,2} .+$")
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".ini", ".cfg",
    ".conf", ".json", ".xml", ".yaml", ".yml", ".py", ".js", ".ts",
    ".html", ".htm", ".css", ".bat", ".ps1", ".sh", ".sql",
}


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def setup_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def long_path(path: str) -> str:
    """Windows 超长路径加 \\\\?\\ 前缀；其他平台原样返回。"""
    if os.name != "nt":
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\?\\"):
        return path
    if len(path) < 248:
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def exists(path: str) -> bool:
    return os.path.exists(long_path(path))


def isfile(path: str) -> bool:
    return os.path.isfile(long_path(path))


def isdir(path: str) -> bool:
    return os.path.isdir(long_path(path))


def is_linkish(path: str) -> bool:
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    try:
        return bool(isjunction and isjunction(path))
    except OSError:
        return False


def is_hidden(path: str) -> bool:
    if os.path.basename(path).startswith("."):
        return True
    if os.name == "nt":
        try:
            attrs = os.stat(long_path(path), follow_symlinks=False).st_file_attributes
            return bool(attrs & statmod.FILE_ATTRIBUTE_HIDDEN) or bool(
                attrs & statmod.FILE_ATTRIBUTE_SYSTEM
            )
        except Exception:
            pass
    return False


def is_category_folder_name(name: str) -> bool:
    return bool(CATEGORY_FOLDER_RE.match(name))


def already_categorized_rel(rel: str) -> bool:
    first = rel.split("/", 1)[0]
    return "/" in rel and is_category_folder_name(first)


def file_sha256(path: str) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(long_path(path), "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


COPY_NAME_RE = re.compile(r" \(\d+\)(\.[^./]+)?$")


def looks_like_windows_copy(path: str) -> bool:
    return bool(COPY_NAME_RE.search(os.path.basename(path)))


def choose_duplicate_keep(paths: list[str]) -> str:
    """优先留下不带 (2) 后缀、路径更短的那一份。"""
    return sorted(
        paths,
        key=lambda p: (1 if looks_like_windows_copy(p) else 0, p.count("/"), natural_key(p)),
    )[0]


def build_duplicate_groups(files: list[dict]) -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for item in files:
        digest = item.get("sha256")
        if not digest or not item.get("size"):
            continue
        buckets.setdefault(digest, []).append(item)
    groups = []
    for digest, items in buckets.items():
        if len(items) < 2:
            continue
        paths = [item["path"] for item in items]
        keep = choose_duplicate_keep(paths)
        extras = [p for p in paths if p != keep]
        extra_set = set(extras)
        for item in items:
            item["duplicate_keep"] = keep
            item["is_duplicate_extra"] = item["path"] in extra_set
        groups.append(
            {
                "sha256": digest,
                "size": items[0]["size"],
                "keep": keep,
                "extras": extras,
                "paths": paths,
            }
        )
    groups.sort(key=lambda g: natural_key(g["keep"]))
    return groups


def journal_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def allocate_json_path(internal: str, prefix: str) -> str:
    """生成不覆盖已有文件的 journal/rollback 路径。"""
    stamp = journal_stamp()
    candidate = os.path.join(internal, f"{prefix}-{stamp}.json")
    index = 1
    while exists(candidate):
        candidate = os.path.join(internal, f"{prefix}-{stamp}-{index}.json")
        index += 1
    return candidate


def is_within(root: str, path: str) -> bool:
    r = os.path.normcase(os.path.abspath(root))
    p = os.path.normcase(os.path.abspath(path))
    try:
        return os.path.commonpath([r, p]) == r
    except ValueError:
        return False


def rel_posix(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def read_json(path: str) -> dict:
    with open(long_path(path), "r", encoding="utf-8-sig") as f:
        return json.load(f)


def write_json(path: str, data) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(long_path(parent), exist_ok=True)
    tmp = path + ".tmp"
    with open(long_path(tmp), "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(long_path(tmp), long_path(path))


def fail(message: str, code: int = 1) -> int:
    print(f"错误: {message}", file=sys.stderr)
    return code


def natural_key(value: str):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", value)]


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

def cmd_scan(args) -> int:
    root = os.path.abspath(args.root)
    if not isdir(root):
        return fail(f"目录不存在或不是文件夹: {root}")

    files, skipped, empty_dirs = [], [], []
    seen_real = set()

    def onerror(err):
        skipped.append({"path": rel_posix(root, err.filename or root), "reason": f"无法访问: {err}"})

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=onerror, followlinks=False):
        dirnames.sort(key=natural_key)
        filenames.sort(key=natural_key)

        real = os.path.realpath(dirpath)
        if real in seen_real:
            dirnames[:] = []
            continue
        seen_real.add(real)

        kept_dirs = []
        for d in dirnames:
            dpath = os.path.join(dirpath, d)
            if dirpath == root and d == INTERNAL_DIR:
                continue
            if d.lower() in ALWAYS_SKIP_DIR_NAMES or d == INTERNAL_DIR:
                skipped.append({"path": rel_posix(root, dpath), "reason": "内部/版本目录，未进入"})
                continue
            if is_linkish(dpath):
                skipped.append({"path": rel_posix(root, dpath), "reason": "符号链接/目录联接，未进入"})
                continue
            if not args.include_hidden and is_hidden(dpath):
                skipped.append({"path": rel_posix(root, dpath), "reason": "隐藏目录，未进入"})
                continue
            kept_dirs.append(d)
        dirnames[:] = kept_dirs

        if not dirnames and not filenames:
            rel = rel_posix(root, dirpath)
            if rel != ".":
                empty_dirs.append(rel)

        for name in filenames:
            fpath = os.path.join(dirpath, name)
            rel = rel_posix(root, fpath)
            if is_linkish(fpath):
                skipped.append({"path": rel, "reason": "符号链接，未处理"})
                continue
            lower = name.lower()
            if lower == ".env" or lower.startswith(".env."):
                skipped.append({"path": rel, "reason": "环境变量/密钥文件，未处理"})
                continue
            if not args.include_hidden and is_hidden(fpath):
                skipped.append({"path": rel, "reason": "隐藏/系统文件，未处理"})
                continue
            if dirpath == root and lower == DEFAULT_REPORT.lower():
                continue
            if lower in {"desktop.ini", "thumbs.db", ".ds_store"}:
                skipped.append({"path": rel, "reason": "系统文件，未处理"})
                continue
            if name.startswith("~$"):
                skipped.append({"path": rel, "reason": "Office 临时文件，未处理"})
                continue
            try:
                st = os.stat(long_path(fpath))
            except OSError as exc:
                skipped.append({"path": rel, "reason": f"无法读取: {exc}"})
                continue
            ext = os.path.splitext(name)[1].lower()
            files.append(
                {
                    "path": rel,
                    "name": name,
                    "ext": ext,
                    "size": st.st_size,
                    "mtime": datetime.datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"),
                    "already_categorized": already_categorized_rel(rel),
                    "sha256": file_sha256(fpath),
                    "duplicate_keep": None,
                    "is_duplicate_extra": False,
                }
            )

    files.sort(key=lambda x: natural_key(x["path"]))
    duplicate_groups = build_duplicate_groups(files)
    extra_count = sum(len(g["extras"]) for g in duplicate_groups)
    by_ext: dict[str, int] = {}
    for f in files:
        by_ext[f["ext"] or "(无扩展名)"] = by_ext.get(f["ext"] or "(无扩展名)", 0) + 1

    result = {
        "root": root,
        "scanned_at": now_iso(),
        "stats": {
            "total_files": len(files),
            "total_size": sum(f["size"] for f in files),
            "by_ext": dict(sorted(by_ext.items())),
            "skipped": len(skipped),
            "empty_dirs": len(empty_dirs),
            "duplicate_groups": len(duplicate_groups),
            "duplicate_extras": extra_count,
        },
        "files": files,
        "skipped": skipped,
        "empty_dirs": empty_dirs,
        "duplicate_groups": duplicate_groups,
    }

    if args.output:
        out = os.path.abspath(args.output)
        if not is_within(root, out):
            return fail(f"--output 必须位于根目录内部: {out}")
        write_json(out, result)
        print(
            f"扫描完成: {len(files)} 个文件, {len(skipped)} 个跳过, {len(empty_dirs)} 个空目录, "
            f"{len(duplicate_groups)} 组内容重复（{extra_count} 个副本）"
        )
        print(f"清单已写入: {out}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# plan 校验与 apply
# ---------------------------------------------------------------------------

def normalize_plan(raw: dict, root: str) -> tuple[list[dict], list[dict], list[dict]]:
    """返回 (categories, moves, unmatched)，非法时抛 ValueError。"""
    plan_root = raw.get("root")
    if plan_root:
        if os.path.normcase(os.path.abspath(plan_root)) != os.path.normcase(root):
            raise ValueError(f"plan.root 与命令行根目录不一致: {plan_root}")

    raw_categories = raw.get("categories")
    if not isinstance(raw_categories, list) or not raw_categories:
        raise ValueError("plan.categories 必须是非空数组")

    categories, seen = [], set()
    for item in raw_categories:
        if isinstance(item, str):
            folder, desc = item, ""
        elif isinstance(item, dict):
            folder, desc = item.get("folder", ""), item.get("description", "")
        else:
            raise ValueError("categories 项必须是字符串或 {folder, description} 对象")
        folder = str(folder).strip()
        if not folder:
            raise ValueError("分类文件夹名不能为空")
        if folder in {".", ".."} or os.path.basename(folder) != folder:
            raise ValueError(f"分类名不能包含路径分隔符: {folder}")
        if any(ch in INVALID_NAME_CHARS for ch in folder) or any(ord(ch) < 32 for ch in folder):
            raise ValueError(f"分类名包含 Windows 非法字符: {folder}")
        if folder.endswith(" ") or folder.endswith("."):
            raise ValueError(f"分类名不能以空格或点结尾: {folder}")
        reserved = {"CON", "PRN", "AUX", "NUL"}
        reserved |= {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
        if folder.split(".")[0].upper() in reserved:
            raise ValueError(f"分类名是 Windows 保留名称: {folder}")
        key = os.path.normcase(folder)
        if key in seen:
            raise ValueError(f"分类名重复: {folder}")
        seen.add(key)
        categories.append({"folder": folder, "description": str(desc)})

    raw_moves = raw.get("moves")
    if not isinstance(raw_moves, list):
        raise ValueError("plan.moves 必须是数组")

    moves, sources = [], set()
    for item in raw_moves:
        if not isinstance(item, dict):
            raise ValueError("moves 项必须是对象")
        rel = str(item.get("path", "")).replace("/", os.sep).strip()
        target = str(item.get("target", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if not rel:
            raise ValueError("moves[].path 不能为空")
        if os.path.isabs(rel):
            raise ValueError(f"moves[].path 必须是相对路径: {rel}")
        norm = os.path.normpath(rel)
        if norm == ".." or norm.startswith(".." + os.sep):
            raise ValueError(f"moves[].path 不能跳出根目录: {rel}")
        src = os.path.abspath(os.path.join(root, norm))
        if not is_within(root, src):
            raise ValueError(f"moves[].path 越出根目录: {rel}")
        if norm.split(os.sep)[0] == INTERNAL_DIR:
            raise ValueError(f"不能移动内部目录中的文件: {rel}")
        if not exists(src):
            raise ValueError(f"源文件不存在: {rel}")
        if not isfile(src):
            raise ValueError(f"源路径不是文件: {rel}")
        if is_linkish(src):
            raise ValueError(f"源文件是符号链接，拒绝移动: {rel}")
        src_key = os.path.normcase(src)
        if src_key in sources:
            raise ValueError(f"源文件重复出现在 moves 中: {rel}")
        sources.add(src_key)
        target_key = os.path.normcase(target)
        if target_key not in seen:
            raise ValueError(f"target 不在 categories 中: {target}")
        # 保留分类的原始大小写
        canonical = next(c["folder"] for c in categories if os.path.normcase(c["folder"]) == target_key)
        moves.append(
            {
                "path": os.path.normpath(rel).replace(os.sep, "/"),
                "src": src,
                "target": canonical,
                "reason": reason,
            }
        )

    unmatched = raw.get("unmatched") or []
    if not isinstance(unmatched, list):
        raise ValueError("plan.unmatched 必须是数组")
    return categories, moves, unmatched


def posix_key(path: str) -> str:
    return os.path.normcase(str(path).replace("\\", "/").strip())


def plan_coverage_error(root: str, moves: list[dict], unmatched: list) -> str | None:
    scan_path = os.path.join(root, INTERNAL_DIR, "scan.json")
    if not isfile(scan_path):
        return "找不到 .organize/scan.json，请先运行 scan"
    try:
        scan = read_json(scan_path)
    except (OSError, json.JSONDecodeError) as exc:
        return f"无法读取 scan.json: {exc}"
    planned = {posix_key(m["path"]) for m in moves}
    for item in unmatched:
        path = item.get("path", item) if isinstance(item, dict) else item
        planned.add(posix_key(path))
    missing = []
    for item in scan.get("files", []):
        rel = item.get("path", "")
        abs_path = os.path.abspath(os.path.join(root, str(rel).replace("/", os.sep)))
        if not isfile(abs_path):
            continue
        if posix_key(rel) not in planned:
            missing.append(rel)
    if missing:
        sample = "、".join(missing[:5])
        extra = f" 等 {len(missing)} 个" if len(missing) > 5 else ""
        return f"plan 未覆盖 scan.json 中仍存在的文件: {sample}{extra}。每个扫描文件都必须进入 moves 或 unmatched"
    return None


def remove_emptied_dirs(root: str, source_files: list[str]) -> list[str]:
    """删除因移动而变空的源目录；预先就空的目录不在 source_files 父链里，会保留。"""
    root_abs = os.path.abspath(root)
    candidates = set()
    for src in source_files:
        parent = os.path.dirname(os.path.abspath(src))
        while parent and os.path.normcase(parent) != os.path.normcase(root_abs):
            candidates.add(parent)
            parent = os.path.dirname(parent)
    removed = []
    for folder in sorted(candidates, key=lambda p: p.count(os.sep), reverse=True):
        try:
            if isdir(folder) and not os.listdir(long_path(folder)):
                os.rmdir(long_path(folder))
                removed.append(rel_posix(root, folder))
        except OSError:
            pass
    return removed


def unique_dest(folder: str, name: str) -> str:
    base, ext = os.path.splitext(name)
    candidate = os.path.join(folder, name)
    index = 2
    while exists(candidate):
        candidate = os.path.join(folder, f"{base} ({index}){ext}")
        index += 1
    return candidate


def print_preview(root: str, categories: list[dict], moves: list[dict], unmatched: list[dict]) -> None:
    print(f"根目录: {root}")
    print("分类方案:")
    for category in categories:
        bucket = [m for m in moves if m["target"] == category["folder"]]
        print(f"├── {category['folder']}（{len(bucket)} 个）")
        for move in bucket:
            reason = f"  — {move['reason']}" if move["reason"] else ""
            print(f"│   ├── {move['path']}{reason}")
    if unmatched:
        print(f"未处理: {len(unmatched)} 个（保留原位）")
        for item in unmatched[:50]:
            reason = f"  — {item.get('reason', '')}" if isinstance(item, dict) else ""
            path = item.get("path", item) if isinstance(item, dict) else item
            print(f"│   ├── {path}{reason}")
        if len(unmatched) > 50:
            print(f"│   └── ... 其余 {len(unmatched) - 50} 个")
    print(f"待移动 {len(moves)} 个文件。")


def cmd_apply(args) -> int:
    root = os.path.abspath(args.root)
    if not isdir(root):
        return fail(f"目录不存在或不是文件夹: {root}")
    plan_path = os.path.abspath(args.plan)
    if not isfile(plan_path):
        return fail(f"plan 文件不存在: {plan_path}")
    try:
        raw = read_json(plan_path)
        categories, moves, unmatched = normalize_plan(raw, root)
    except (ValueError, json.JSONDecodeError) as exc:
        return fail(f"plan 校验失败: {exc}")
    coverage = plan_coverage_error(root, moves, unmatched)
    if coverage:
        return fail(coverage)

    print_preview(root, categories, moves, unmatched)

    if not args.apply:
        print("（dry-run）未修改任何文件。确认后加 --apply 执行。")
        return 0

    if not moves:
        print("没有可移动的文件，未做改动。")
        return 0

    internal = os.path.join(root, INTERNAL_DIR)
    os.makedirs(long_path(internal), exist_ok=True)
    journal_path = allocate_json_path(internal, "journal")
    journal_name = os.path.basename(journal_path)
    journal = {
        "version": 1,
        "root": root,
        "started_at": now_iso(),
        "finished_at": None,
        "report": None,
        "created_dirs": [],
        "removed_empty_dirs": [],
        "moves": [],
    }

    success = skipped = failed = 0
    created_dirs = set()

    for move in moves:
        src = move["src"]
        rel_src = rel_posix(root, src)
        target_dir = os.path.join(root, move["target"])
        entry = {
            "src": rel_src,
            "dest": None,
            "target": move["target"],
            "reason": move["reason"],
            "status": "failed",
            "error": None,
        }
        try:
            if not is_within(root, target_dir) or os.path.basename(target_dir) != move["target"]:
                raise ValueError("目标分类路径非法")
            if exists(target_dir) and not isdir(target_dir):
                raise ValueError(f"目标分类位置已被同名文件占用: {move['target']}")
            if os.path.normcase(os.path.dirname(src)) == os.path.normcase(target_dir):
                entry["status"] = "skipped"
                entry["error"] = "已在目标分类中"
                skipped += 1
                journal["moves"].append(entry)
                write_json(journal_path, journal)
                continue
            if not isdir(target_dir):
                os.makedirs(long_path(target_dir), exist_ok=False)
                created_dirs.add(os.path.normcase(target_dir))
                journal["created_dirs"] = sorted(created_dirs)
            dest = unique_dest(target_dir, os.path.basename(src))
            shutil.move(long_path(src), long_path(dest))
            entry["status"] = "moved"
            entry["dest"] = rel_posix(root, dest)
            success += 1
        except Exception as exc:  # 单个文件失败不阻断整体
            entry["status"] = "failed"
            entry["error"] = str(exc)
            failed += 1
        journal["moves"].append(entry)
        write_json(journal_path, journal)

    moved_sources = [
        os.path.join(root, m["src"].replace("/", os.sep))
        for m in journal["moves"]
        if m.get("status") == "moved"
    ]
    journal["removed_empty_dirs"] = remove_emptied_dirs(root, moved_sources)
    journal["finished_at"] = now_iso()
    report_name = args.report_name
    if report_name in {".", ".."} or os.path.basename(report_name) != report_name:
        return fail("--report-name 只能是文件名")
    report_path = os.path.join(root, report_name)
    journal["report"] = report_name
    write_json(journal_path, journal)
    write_json(os.path.join(internal, "latest-journal.json"), {"journal": journal_name})
    write_report(report_path, root, categories, journal, unmatched)

    print(f"完成: 成功 {success}，跳过 {skipped}，失败 {failed}")
    if journal.get("removed_empty_dirs"):
        print("已删除空源目录: " + "、".join(journal["removed_empty_dirs"]))
    print(f"整理报告: {report_path}")
    print(f"执行记录: {journal_path}")
    if failed:
        print("存在失败项，请查看报告或 journal。", file=sys.stderr)
        return 2
    return 0


# ---------------------------------------------------------------------------
# verify / rollback
# ---------------------------------------------------------------------------

def resolve_journal(root: str, value: str) -> str:
    if value == "latest":
        pointer = os.path.join(root, INTERNAL_DIR, "latest-journal.json")
        if not isfile(pointer):
            raise FileNotFoundError(f"找不到 latest-journal.json: {pointer}")
        name = read_json(pointer).get("journal")
        if not name:
            raise ValueError("latest-journal.json 缺少 journal 字段")
        return os.path.join(root, INTERNAL_DIR, name)
    return value if os.path.isabs(value) else os.path.join(root, value)


def cmd_verify(args) -> int:
    root = os.path.abspath(args.root)
    try:
        journal_path = resolve_journal(root, args.journal)
        journal = read_json(journal_path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        return fail(str(exc))

    moved = [m for m in journal.get("moves", []) if m.get("status") == "moved"]
    missing = []
    for move in moved:
        dest = move.get("dest")
        if not dest:
            missing.append((move, "记录缺少 dest"))
            continue
        dest_abs = os.path.abspath(os.path.join(root, dest.replace("/", os.sep)))
        if not isfile(dest_abs):
            missing.append((move, "目标文件不存在"))
    print(f"journal: {journal_path}")
    print(f"已移动记录: {len(moved)}，校验通过: {len(moved) - len(missing)}，异常: {len(missing)}")
    for move, reason in missing:
        print(f"  - {move.get('src')} -> {move.get('dest')}: {reason}")
    return 1 if missing else 0


def cmd_rollback(args) -> int:
    root = os.path.abspath(args.root)
    try:
        journal_path = resolve_journal(root, args.journal)
        journal = read_json(journal_path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        return fail(str(exc))

    moves = [m for m in journal.get("moves", []) if m.get("status") == "moved"]
    if not moves:
        print("journal 中没有可回滚的移动记录。")
        return 0

    planned, skipped = [], []
    for move in reversed(moves):
        dest_rel = move.get("dest")
        src_rel = move.get("src")
        if not dest_rel or not src_rel:
            skipped.append((move, "记录缺少路径"))
            continue
        dest_abs = os.path.abspath(os.path.join(root, dest_rel.replace("/", os.sep)))
        src_abs = os.path.abspath(os.path.join(root, src_rel.replace("/", os.sep)))
        if not isfile(dest_abs):
            skipped.append((move, "目标位置已无该文件"))
            continue
        if exists(src_abs):
            skipped.append((move, "原位置已有同名文件，未覆盖"))
            continue
        planned.append((move, src_abs, dest_abs))

    print(f"将回滚 {len(planned)} 个文件，跳过 {len(skipped)} 个。")
    for move, src_abs, dest_abs in planned:
        print(f"  {move['dest']} -> {move['src']}")
    if not args.apply:
        print("（dry-run）未修改任何文件。确认后加 --apply 执行。")
        return 0

    done, failed = [], []
    for move, src_abs, dest_abs in planned:
        try:
            os.makedirs(long_path(os.path.dirname(src_abs)), exist_ok=True)
            shutil.move(long_path(dest_abs), long_path(src_abs))
            done.append(move)
        except Exception as exc:
            failed.append((move, str(exc)))

    removed_dirs = []
    for dir_key in journal.get("created_dirs", []):
        try:
            if os.path.isdir(dir_key) and not os.listdir(long_path(dir_key)):
                os.rmdir(long_path(dir_key))
                removed_dirs.append(dir_key)
        except OSError:
            pass

    restored_keys = {(m.get("src"), m.get("dest")) for m in done}
    for entry in journal.get("moves", []):
        if entry.get("status") == "moved" and (entry.get("src"), entry.get("dest")) in restored_keys:
            entry["status"] = "rolled_back"
    journal["rolled_back_at"] = now_iso()
    write_json(journal_path, journal)

    record = {
        "rolled_back_at": journal["rolled_back_at"],
        "journal": journal_path,
        "restored": [{"src": m["src"], "dest": m["dest"]} for m in done],
        "skipped": [{"src": m.get("src"), "dest": m.get("dest"), "reason": r} for m, r in skipped],
        "failed": [{"src": m.get("src"), "dest": m.get("dest"), "error": e} for m, e in failed],
        "removed_created_dirs": removed_dirs,
    }
    record_path = allocate_json_path(os.path.join(root, INTERNAL_DIR), "rollback")
    write_json(record_path, record)

    print(f"回滚完成: 成功 {len(done)}，失败 {len(failed)}，跳过 {len(skipped)}")
    print(f"回滚记录: {record_path}")
    if failed:
        for move, error in failed:
            print(f"  - {move.get('src')}: {error}", file=sys.stderr)
        return 2
    return 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def md_cell(value) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def write_report(path: str, root: str, categories: list[dict], journal: dict, unmatched: list[dict]) -> None:
    moves = journal.get("moves", [])
    moved = [m for m in moves if m["status"] == "moved"]
    failed = [m for m in moves if m["status"] == "failed"]
    skipped = [m for m in moves if m["status"] == "skipped"]

    counts = {}
    for move in moved:
        counts[move["target"]] = counts.get(move["target"], 0) + 1

    lines = [
        "# 整理报告",
        "",
        f"- 整理目录：`{root}`",
        f"- 执行时间：{journal.get('started_at', '')} → {journal.get('finished_at', '')}",
        f"- 计划分类：{len(categories)} 类",
        f"- 移动结果：成功 {len(moved)}，跳过 {len(skipped)}，失败 {len(failed)}",
        f"- 删除空源目录：{len(journal.get('removed_empty_dirs') or [])} 个",
        "",
        "## 分类统计",
        "",
        "| 分类 | 移动文件数 |",
        "| --- | --- |",
    ]
    for category in categories:
        lines.append(f"| {md_cell(category['folder'])} | {counts.get(category['folder'], 0)} |")

    lines += ["", "## 移动明细", "", "| 原路径 | 新路径 | 判定依据 |", "| --- | --- | --- |"]
    for move in moved:
        lines.append(
            f"| {md_cell(move['src'])} | {md_cell(move['dest'])} | {md_cell(move.get('reason', ''))} |"
        )
    if not moved:
        lines.append("| （无） | - | - |")

    if failed or skipped:
        lines += ["", "## 未移动 / 异常", "", "| 原路径 | 原因 |", "| --- | --- |"]
        for move in skipped:
            lines.append(f"| {md_cell(move['src'])} | 跳过：{md_cell(move.get('error') or '')} |")
        for move in failed:
            lines.append(f"| {md_cell(move['src'])} | 失败：{md_cell(move.get('error') or '')} |")

    if unmatched:
        lines += ["", "## 未识别清单", "", "| 文件 | 说明 |", "| --- | --- |"]
        for item in unmatched:
            if isinstance(item, dict):
                lines.append(f"| {md_cell(item.get('path', ''))} | {md_cell(item.get('reason', ''))} |")
            else:
                lines.append(f"| {md_cell(item)} | 未识别，保留原位 |")

    scan_path = os.path.join(root, INTERNAL_DIR, "scan.json")
    groups = []
    if isfile(scan_path):
        try:
            groups = read_json(scan_path).get("duplicate_groups") or []
        except (OSError, json.JSONDecodeError, ValueError):
            groups = []
    if groups:
        lines += ["", "## 内容重复（SHA-256 相同，未删除）", "", "| 保留 | 副本 |", "| --- | --- |"]
        for group in groups:
            extras = "、".join(group.get("extras") or [])
            lines.append(f"| {md_cell(group.get('keep', ''))} | {md_cell(extras)} |")

    script_path = os.path.abspath(__file__).replace("\\", "/")
    lines += [
        "",
        "## 回滚",
        "",
        "```bash",
        f'python "{script_path}" rollback "{root}" --journal latest --apply',
        "```",
        "",
        "回滚按执行记录逆序移回；原位置已有同名文件时不会覆盖。",
        "",
    ]

    with open(long_path(path), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Auto-Organizer 机械执行器")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="扫描目录并输出文件清单")
    p_scan.add_argument("root")
    p_scan.add_argument("--output", help="清单输出路径（必须在根目录内部）")
    p_scan.add_argument("--include-hidden", action="store_true", help="包含隐藏/系统文件")
    p_scan.set_defaults(func=cmd_scan)

    p_apply = sub.add_parser("apply", help="按 plan 移动文件；默认 dry-run")
    p_apply.add_argument("root")
    p_apply.add_argument("--plan", required=True)
    p_apply.add_argument("--apply", action="store_true", help="真正执行移动")
    p_apply.add_argument("--report-name", default=DEFAULT_REPORT)
    p_apply.set_defaults(func=cmd_apply)

    p_verify = sub.add_parser("verify", help="校验已移动文件是否在目标位置")
    p_verify.add_argument("root")
    p_verify.add_argument("--journal", default="latest")
    p_verify.set_defaults(func=cmd_verify)

    p_rollback = sub.add_parser("rollback", help="按 journal 回滚；默认 dry-run")
    p_rollback.add_argument("root")
    p_rollback.add_argument("--journal", default="latest")
    p_rollback.add_argument("--apply", action="store_true", help="真正执行回滚")
    p_rollback.set_defaults(func=cmd_rollback)
    return parser


def main() -> int:
    setup_stdout()
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
