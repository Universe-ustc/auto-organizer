#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""auto-organizer 回归测试（仅标准库）。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


organize = load_module("organize", SCRIPTS / "organize.py")
extract_text = load_module("extract_text", SCRIPTS / "extract_text.py")


def ns(**kwargs):
    return argparse.Namespace(**kwargs)


def write(path: Path, text: str = "x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class OrganizerTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="ao-test-"))
        self.root = self.work / "inbox"
        self.root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def scan(self, include_hidden=False):
        out = self.root / ".organize" / "scan.json"
        rc = organize.cmd_scan(
            ns(root=str(self.root), output=str(out), include_hidden=include_hidden)
        )
        self.assertEqual(rc, 0)
        return json.loads(out.read_text(encoding="utf-8"))

    def write_plan(self, name, categories, moves, unmatched=None):
        plan = {
            "root": str(self.root),
            "categories": categories,
            "moves": moves,
            "unmatched": unmatched or [],
        }
        path = self.root / ".organize" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        return path

    def apply(self, plan_path, do_apply=True):
        return organize.cmd_apply(
            ns(
                root=str(self.root),
                plan=str(plan_path),
                apply=do_apply,
                report_name=organize.DEFAULT_REPORT,
            )
        )

    def test_dotfile_skipped_without_windows_hidden_attribute(self):
        write(self.root / ".env", "SECRET=1")
        write(self.root / "notes.txt", "ok")
        data = self.scan()
        paths = {f["path"] for f in data["files"]}
        skipped = {s["path"] for s in data["skipped"]}
        self.assertIn("notes.txt", paths)
        self.assertNotIn(".env", paths)
        self.assertIn(".env", skipped)

    def test_git_dir_always_skipped(self):
        write(self.root / ".git" / "HEAD", "ref: refs/heads/main")
        write(self.root / "readme.txt", "ok")
        data = self.scan(include_hidden=True)
        paths = {f["path"] for f in data["files"]}
        self.assertIn("readme.txt", paths)
        self.assertTrue(all(not p.startswith(".git/") and p != ".git/HEAD" for p in paths))

    def test_already_categorized_flag(self):
        write(self.root / "1 活动策划" / "旧策划.txt", "old")
        write(self.root / "2024 年报" / "年报.txt", "year")
        write(self.root / "散落文件.txt", "new")
        data = self.scan()
        by_path = {f["path"]: f for f in data["files"]}
        self.assertTrue(by_path["1 活动策划/旧策划.txt"]["already_categorized"])
        self.assertFalse(by_path["2024 年报/年报.txt"]["already_categorized"])
        self.assertFalse(by_path["散落文件.txt"]["already_categorized"])

    def test_env_skipped_even_with_include_hidden(self):
        write(self.root / ".env", "SECRET=1")
        write(self.root / "notes.txt", "ok")
        data = self.scan(include_hidden=True)
        paths = {f["path"] for f in data["files"]}
        skipped = {s["path"] for s in data["skipped"]}
        self.assertIn("notes.txt", paths)
        self.assertNotIn(".env", paths)
        self.assertIn(".env", skipped)

    def test_journal_second_apply_does_not_overwrite(self):
        write(self.root / "a.txt", "A")
        write(self.root / "b.txt", "B")
        self.scan()
        with patch.object(organize, "journal_stamp", return_value="20260101-120000"):
            plan1 = self.write_plan(
                "p1.json",
                [{"folder": "1 甲", "description": "a"}],
                [{"path": "a.txt", "target": "1 甲", "reason": "a"}],
                [{"path": "b.txt", "reason": "稍后"}],
            )
            self.assertEqual(self.apply(plan1), 0)
            plan2 = self.write_plan(
                "p2.json",
                [{"folder": "2 乙", "description": "b"}],
                [{"path": "b.txt", "target": "2 乙", "reason": "b"}],
            )
            self.assertEqual(self.apply(plan2), 0)

        journals = sorted((self.root / ".organize").glob("journal-*.json"))
        self.assertGreaterEqual(len(journals), 2, "第二次 apply 覆盖了第一份 journal")
        payloads = [json.loads(p.read_text(encoding="utf-8")) for p in journals]
        moved_src = {
            m["src"]
            for payload in payloads
            for m in payload.get("moves", [])
            if m.get("status") == "moved"
        }
        self.assertIn("a.txt", moved_src)
        self.assertIn("b.txt", moved_src)

    def test_empty_source_dir_removed_but_preexisting_empty_kept(self):
        write(self.root / "新建文件夹" / "场地.txt", "场地申请")
        (self.root / "空文件夹").mkdir()
        self.scan()
        plan = self.write_plan(
            "plan.json",
            [{"folder": "1 场地申请", "description": "x"}],
            [{"path": "新建文件夹/场地.txt", "target": "1 场地申请", "reason": "场地"}],
        )
        self.assertEqual(self.apply(plan), 0)
        self.assertFalse((self.root / "新建文件夹").exists())
        self.assertTrue((self.root / "空文件夹").is_dir())
        self.assertTrue((self.root / "1 场地申请" / "场地.txt").exists())

    def test_incomplete_plan_rejected_when_scan_exists(self):
        write(self.root / "a.txt", "A")
        write(self.root / "b.txt", "B")
        self.scan()
        plan = self.write_plan(
            "plan.json",
            [{"folder": "1 临时", "description": "x"}],
            [{"path": "a.txt", "target": "1 临时", "reason": "只移一个"}],
        )
        rc = self.apply(plan, do_apply=False)
        self.assertEqual(rc, 1)

    def test_apply_without_scan_rejected(self):
        write(self.root / "a.txt", "A")
        plan = self.write_plan(
            "plan.json",
            [{"folder": "1 临时", "description": "x"}],
            [{"path": "a.txt", "target": "1 临时", "reason": "a"}],
        )
        rc = self.apply(plan, do_apply=False)
        self.assertEqual(rc, 1)

    def test_rollback_updates_journal_and_verify_passes(self):
        write(self.root / "a.txt", "A")
        self.scan()
        plan = self.write_plan(
            "plan.json",
            [{"folder": "1 临时", "description": "x"}],
            [{"path": "a.txt", "target": "1 临时", "reason": "a"}],
        )
        self.assertEqual(self.apply(plan), 0)
        rc = organize.cmd_rollback(ns(root=str(self.root), journal="latest", apply=True))
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "a.txt").exists())
        self.assertFalse((self.root / "1 临时" / "a.txt").exists())
        self.assertEqual(organize.cmd_verify(ns(root=str(self.root), journal="latest")), 0)
        journal_path = organize.resolve_journal(str(self.root), "latest")
        journal = json.loads(Path(journal_path).read_text(encoding="utf-8"))
        self.assertTrue(any(m.get("status") == "rolled_back" for m in journal["moves"]))
        self.assertFalse((self.root / "1 临时").exists())

    def test_rollback_restores_nested_and_duplicate_names(self):
        write(self.root / "新建文件夹" / "场地.txt", "场地")
        write(self.root / "nested" / "report.txt", "A")
        write(self.root / "other" / "report.txt", "B")
        self.scan()
        plan = self.write_plan(
            "plan.json",
            [
                {"folder": "1 场地申请", "description": "x"},
                {"folder": "2 活动总结", "description": "y"},
            ],
            [
                {"path": "新建文件夹/场地.txt", "target": "1 场地申请", "reason": "场地"},
                {"path": "nested/report.txt", "target": "2 活动总结", "reason": "A"},
                {"path": "other/report.txt", "target": "2 活动总结", "reason": "B"},
            ],
        )
        self.assertEqual(self.apply(plan), 0)
        self.assertFalse((self.root / "新建文件夹").exists())
        self.assertTrue((self.root / "2 活动总结" / "report.txt").exists())
        self.assertTrue((self.root / "2 活动总结" / "report (2).txt").exists())

        dry = organize.cmd_rollback(ns(root=str(self.root), journal="latest", apply=False))
        self.assertEqual(dry, 0)
        self.assertTrue((self.root / "1 场地申请" / "场地.txt").exists())

        rc = organize.cmd_rollback(ns(root=str(self.root), journal="latest", apply=True))
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "新建文件夹" / "场地.txt").exists())
        self.assertTrue((self.root / "nested" / "report.txt").exists())
        self.assertTrue((self.root / "other" / "report.txt").exists())
        self.assertEqual((self.root / "nested" / "report.txt").read_text(encoding="utf-8"), "A")
        self.assertEqual((self.root / "other" / "report.txt").read_text(encoding="utf-8"), "B")
        self.assertFalse((self.root / "1 场地申请").exists())
        self.assertEqual(organize.cmd_verify(ns(root=str(self.root), journal="latest")), 0)

    def test_path_traversal_rejected(self):
        write(self.root / "a.txt", "A")
        self.scan()
        plan = self.write_plan(
            "evil.json",
            [{"folder": "1 临时", "description": "x"}],
            [{"path": "../escape.txt", "target": "1 临时", "reason": "x"}],
        )
        rc = self.apply(plan, do_apply=False)
        self.assertEqual(rc, 1)

    def test_extract_plain_and_old_office_note(self):
        txt = self.root / "策划.txt"
        write(txt, "这是策划案")
        old = self.root / "old.doc"
        old.write_bytes(b"\xd0\xcf\x11\xe0")
        results = [extract_text.extract_one(str(txt), 100), extract_text.extract_one(str(old), 100)]
        self.assertIn("策划", results[0]["text"])
        self.assertEqual(results[1]["type"], "office-old")

    def test_xlsx_keeps_empty_columns(self):
        path = self.root / "sparse.xlsx"
        ns_ss = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        shared = ["物品", "金额"]
        sst = "".join(f"<si><t>{s}</t></si>" for s in shared)
        sheet = (
            f'<?xml version="1.0"?><worksheet xmlns="{ns_ss}"><sheetData>'
            '<row r="1">'
            '<c r="A1" t="s"><v>0</v></c>'
            '<c r="C1" t="s"><v>1</v></c>'
            "</row></sheetData></worksheet>"
        )
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("xl/sharedStrings.xml", f'<?xml version="1.0"?><sst xmlns="{ns_ss}">{sst}</sst>')
            z.writestr("xl/worksheets/sheet1.xml", sheet)
        text = extract_text.extract_one(str(path), 200)["text"]
        self.assertIn("物品", text)
        self.assertIn("金额", text)
        self.assertIn("\t\t", text)

    def test_scan_hashes_and_duplicate_groups(self):
        write(self.root / "讲义.pdf", "SAME")
        write(self.root / "讲义 (1).pdf", "SAME")
        write(self.root / "不同.pdf", "OTHER")
        data = self.scan()
        groups = data["duplicate_groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["keep"], "讲义.pdf")
        self.assertEqual(groups[0]["extras"], ["讲义 (1).pdf"])
        by_path = {f["path"]: f for f in data["files"]}
        self.assertTrue(by_path["讲义 (1).pdf"]["is_duplicate_extra"])
        self.assertFalse(by_path["讲义.pdf"]["is_duplicate_extra"])
        self.assertIsNone(by_path["不同.pdf"]["duplicate_keep"])
        self.assertEqual(data["stats"]["duplicate_extras"], 1)

    def test_duplicate_extras_kept_as_copies_not_deleted(self):
        write(self.root / "讲义.pdf", "SAME")
        write(self.root / "讲义 (1).pdf", "SAME")
        self.scan()
        plan = self.write_plan(
            "plan.json",
            [
                {"folder": "1 活动课件", "description": "x"},
                {"folder": "2 重复文件", "description": "内容相同的副本"},
            ],
            [
                {"path": "讲义.pdf", "target": "1 活动课件", "reason": "主题讲义"},
                {"path": "讲义 (1).pdf", "target": "2 重复文件", "reason": "与 讲义.pdf 内容相同"},
            ],
        )
        self.assertEqual(self.apply(plan), 0)
        self.assertTrue((self.root / "1 活动课件" / "讲义.pdf").exists())
        self.assertTrue((self.root / "2 重复文件" / "讲义 (1).pdf").exists())
        self.assertEqual((self.root / "1 活动课件" / "讲义.pdf").read_text(encoding="utf-8"), "SAME")
        self.assertEqual((self.root / "2 重复文件" / "讲义 (1).pdf").read_text(encoding="utf-8"), "SAME")
        report = (self.root / "_整理报告.md").read_text(encoding="utf-8")
        self.assertIn("内容重复", report)

    def test_ocr_skipped_without_tesseract(self):
        png = self.root / "tiny.png"
        png.write_bytes(
            bytes.fromhex(
                "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
                "0000000c4944415408d763f8ffff3f0005fe02fe0d6d81890000000049454e44ae426082"
            )
        )
        extract_text._TESSERACT_CACHE = None
        with patch.object(extract_text, "find_tesseract", return_value=None):
            result = extract_text.extract_one(str(png), 200)
        joined = " ".join(result["notes"])
        self.assertIn("未 OCR", joined)
        self.assertIn("Tesseract", joined)
        self.assertIn("未安装", joined)
        extract_text._TESSERACT_CACHE = None


if __name__ == "__main__":
    unittest.main()
