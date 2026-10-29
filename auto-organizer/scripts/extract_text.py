#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多格式文本抽取（仅标准库，可选库自动降级）。

支持：
- 纯文本：txt/md/csv/json/xml/代码等（多编码尝试）
- docx/pptx/xlsx：直接解析 OOXML（zip + XML）
- pdf：优先 pypdf/PyPDF2（可选依赖）；未安装时做基础流解析
- 图片：JPEG EXIF、PNG 尺寸；本机有 Tesseract OCR 则识别文字，没有则注明缺的是什么
- zip：列出压缩包内文件名
- 旧版 doc/xls/ppt、7z/rar、音视频：返回元信息并注明不支持

输出 JSON，便于调用方解析。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
import zlib

TEXT_EXT = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".ini", ".cfg",
    ".conf", ".json", ".xml", ".yaml", ".yml", ".py", ".js", ".ts",
    ".html", ".htm", ".css", ".bat", ".ps1", ".sh", ".sql",
}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic"}
OCR_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
_TESSERACT_CACHE = None
NOTE_OCR_MISSING_TESSERACT = (
    "未 OCR：本机未安装或未找到 Tesseract OCR（命令 tesseract 不在 PATH）。"
    "安装后即可识别 jpg/png 等图片文字：https://github.com/tesseract-ocr/tesseract"
)
NOTE_OCR_PDF = (
    "未 OCR：扫描件 PDF 需要先转成图片。当前脚本不会把 PDF 交给 Tesseract"
    "（Tesseract 只处理图片）；本机即使已安装 Tesseract 也不会自动识别 PDF。"
)
NOTE_OCR_UNSUPPORTED_IMAGE = (
    "未 OCR：该图片格式不在当前 OCR 范围内（仅 jpg/jpeg/png/webp/bmp/tif/tiff）。"
    "不是缺软件，而是脚本未调用 Tesseract。"
)
OFFICE_OLD = {".doc", ".xls", ".ppt"}
ARCHIVE_EXT = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"}
MEDIA_EXT = {".mp4", ".avi", ".mov", ".mkv", ".flv", ".wmv", ".mp3", ".wav", ".flac", ".aac", ".m4a"}


def setup_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def iso_time(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def basic_meta(path: str) -> dict:
    try:
        st = os.stat(path)
        return {
            "size": st.st_size,
            "mtime": iso_time(st.st_mtime),
            "ext": os.path.splitext(path)[1].lower(),
        }
    except OSError as exc:
        return {"error": str(exc), "ext": os.path.splitext(path)[1].lower()}


# ---------------------------------------------------------------------------
# 纯文本
# ---------------------------------------------------------------------------

def extract_plain(path: str, max_chars: int) -> tuple[str, list[str]]:
    with open(path, "rb") as f:
        raw = f.read(2_000_000)
    notes = []
    if len(raw) >= 2_000_000:
        notes.append("文件较大，仅读取前 2 MB")
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)[:max_chars], notes
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")[:max_chars], notes + ["编码无法确定，已用替换字符解码"]


# ---------------------------------------------------------------------------
# OOXML
# ---------------------------------------------------------------------------

def xml_text(elem, tag: str) -> str:
    node = elem.find(tag)
    return (node.text or "").strip() if node is not None else ""


def extract_docx(path: str, max_chars: int) -> tuple[str, dict, list[str]]:
    from xml.etree import ElementTree as ET

    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    dc = "{http://purl.org/dc/elements/1.1/}"
    dcterms = "{http://purl.org/dc/terms/}"
    meta, notes, paragraphs = {}, [], []
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if "word/document.xml" not in names:
            return "", meta, ["不是有效的 docx：缺少 word/document.xml"]
        root = ET.fromstring(z.read("word/document.xml"))
        for para in root.iter(w + "p"):
            parts = []
            for node in para.iter():
                if node.tag == w + "t" and node.text:
                    parts.append(node.text)
                elif node.tag == w + "tab":
                    parts.append("\t")
                elif node.tag in (w + "br", w + "cr"):
                    parts.append("\n")
            paragraphs.append("".join(parts))
        if "docProps/core.xml" in names:
            try:
                core = ET.fromstring(z.read("docProps/core.xml"))
                meta = {
                    "title": xml_text(core, dc + "title"),
                    "creator": xml_text(core, dc + "creator"),
                    "created": xml_text(core, dcterms + "created"),
                    "modified": xml_text(core, dcterms + "modified"),
                }
                meta = {k: v for k, v in meta.items() if v}
            except ET.ParseError:
                notes.append("core.xml 解析失败")
    text = "\n".join(paragraphs)
    if not text.strip():
        notes.append("文档正文为空")
    return text[:max_chars], meta, notes


def extract_pptx(path: str, max_chars: int) -> tuple[str, dict, list[str]]:
    from xml.etree import ElementTree as ET

    a = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    meta, notes, slides = {}, [], []
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
        names.sort(key=lambda n: int(re.search(r"(\d+)", os.path.basename(n)).group(1)))
        if not names:
            return "", meta, ["不是有效的 pptx：未找到幻灯片"]
        for index, name in enumerate(names, 1):
            root = ET.fromstring(z.read(name))
            paragraphs = []
            for para in root.iter(a + "p"):
                parts = [node.text for node in para.iter(a + "t") if node.text]
                if parts:
                    paragraphs.append("".join(parts))
            if paragraphs:
                slides.append(f"[Slide {index}]\n" + "\n".join(paragraphs))
    return "\n\n".join(slides)[:max_chars], meta, notes


def col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        if not ("A" <= ch <= "Z"):
            return 0
        n = n * 26 + (ord(ch) - 64)
    return max(n - 1, 0)


def extract_xlsx(path: str, max_chars: int) -> tuple[str, dict, list[str]]:
    from xml.etree import ElementTree as ET

    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    notes, output = [], []
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.iter(ns + "si"):
                shared.append("".join(t.text or "" for t in si.iter(ns + "t")))
        sheets = sorted(
            (n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
            key=lambda n: int(re.search(r"(\d+)", os.path.basename(n)).group(1)),
        )
        if not sheets:
            return "", {}, ["不是有效的 xlsx：未找到工作表"]
        for index, name in enumerate(sheets, 1):
            root = ET.fromstring(z.read(name))
            rows = []
            for row in root.iter(ns + "row"):
                cells = []
                for cell in row.findall(ns + "c"):
                    cell_type = cell.get("t")
                    value_node = cell.find(ns + "v")
                    value = value_node.text if value_node is not None and value_node.text else ""
                    if cell_type == "s":
                        try:
                            value = shared[int(value)]
                        except (ValueError, IndexError):
                            value = ""
                    elif cell_type == "inlineStr":
                        inline = cell.find(ns + "is")
                        value = "".join(t.text or "" for t in inline.iter(ns + "t")) if inline is not None else ""
                    ref = cell.get("r") or ""
                    match = re.match(r"([A-Za-z]+)", ref)
                    idx = col_index(match.group(1)) if match else len(cells)
                    while len(cells) <= idx:
                        cells.append("")
                    cells[idx] = value
                if any(cells):
                    rows.append("\t".join(cells))
            if rows:
                output.append(f"[Sheet {index}]\n" + "\n".join(rows))
    return "\n\n".join(output)[:max_chars], {}, notes


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def pdf_unescape(text: str) -> str:
    replacements = {
        "\\n": "\n", "\\r": "\r", "\\t": "\t", "\\b": "\b", "\\f": "\f",
        "\\(": "(", "\\)": ")", "\\\\": "\\",
    }
    for key, value in replacements.items():
        text = text.replace(key, value)

    def octal(match):
        try:
            return chr(int(match.group(1), 8))
        except ValueError:
            return ""

    return re.sub(r"\\([0-7]{1,3})", octal, text)


def pdf_text_from_content(data: bytes) -> str:
    content = data.decode("latin-1", errors="ignore")
    pieces = []
    for match in re.finditer(r"\((?:\\.|[^()\\])*\)\s*Tj", content):
        literal = match.group(0).rsplit(")", 1)[0][1:]
        pieces.append(pdf_unescape(literal))
    for match in re.finditer(r"\[(.*?)\]\s*TJ", content, re.S):
        literals = re.findall(r"\((?:\\.|[^()\\])*\)", match.group(1))
        pieces.append("".join(pdf_unescape(item[1:-1]) for item in literals))
    for match in re.finditer(r"<([0-9A-Fa-f\s]+)>\s*Tj", content):
        hex_text = re.sub(r"\s+", "", match.group(1))
        if len(hex_text) % 2 == 0:
            try:
                pieces.append(bytes.fromhex(hex_text).decode("utf-16-be", errors="ignore"))
            except ValueError:
                pass
    return " ".join(piece for piece in pieces if piece.strip())


def extract_pdf(path: str, max_chars: int) -> tuple[str, dict, list[str]]:
    notes = []
    reader_class = None
    try:
        from pypdf import PdfReader as reader_class  # type: ignore
    except ImportError:
        try:
            from PyPDF2 import PdfReader as reader_class  # type: ignore
        except ImportError:
            reader_class = None

    if reader_class is not None:
        try:
            reader = reader_class(path)
            pages = []
            for index, page in enumerate(reader.pages[:20], 1):
                pages.append(f"[Page {index}]\n{page.extract_text() or ''}")
            if len(reader.pages) > 20:
                notes.append(f"PDF 共 {len(reader.pages)} 页，仅读取前 20 页")
            text = "\n\n".join(pages)
            if not text.strip():
                notes.append("PDF 未解析出文本，可能为扫描件。" + NOTE_OCR_PDF)
            return text[:max_chars], {"pages": len(reader.pages)}, notes
        except Exception as exc:
            notes.append(f"pypdf 解析失败，改用基础解析: {exc}")

    with open(path, "rb") as f:
        data = f.read(20_000_000)
    pieces = []
    for match in re.finditer(rb"stream\r?\n", data):
        start = match.end()
        end = data.find(b"endstream", start)
        if end == -1:
            continue
        raw = data[start:end].rstrip(b"\r\n")
        chunk = None
        for wbits in (15, -15):
            try:
                chunk = zlib.decompress(raw, wbits)
                break
            except zlib.error:
                continue
        if chunk is None:
            if b"BT" in raw or b"Tj" in raw or b"TJ" in raw:
                chunk = raw
            else:
                continue
        text = pdf_text_from_content(chunk)
        if text.strip():
            pieces.append(text)
    text = "\n".join(pieces)
    if not text.strip():
        notes.append("未能抽取 PDF 文本：可能为扫描件、加密或特殊字体。" + NOTE_OCR_PDF)
    else:
        notes.append("未安装 pypdf，使用基础流解析，结果可能不完整")
    return text[:max_chars], {}, notes


# ---------------------------------------------------------------------------
# 图片 EXIF / PNG
# ---------------------------------------------------------------------------

def read_tiff_ifd(tiff: bytes, offset: int, endian: str) -> dict:
    result = {}
    if offset < 0 or offset + 2 > len(tiff):
        return result
    count = int.from_bytes(tiff[offset:offset + 2], endian)
    type_sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}
    for index in range(count):
        entry = offset + 2 + index * 12
        if entry + 12 > len(tiff):
            break
        tag = int.from_bytes(tiff[entry:entry + 2], endian)
        value_type = int.from_bytes(tiff[entry + 2:entry + 4], endian)
        value_count = int.from_bytes(tiff[entry + 4:entry + 8], endian)
        size = type_sizes.get(value_type, 1) * value_count
        if size <= 4:
            raw = tiff[entry + 8:entry + 8 + size]
        else:
            value_offset = int.from_bytes(tiff[entry + 8:entry + 12], endian)
            raw = tiff[value_offset:value_offset + size]
        value = None
        try:
            if value_type == 2:
                value = raw.split(b"\x00")[0].decode("utf-8", errors="ignore").strip()
            elif value_type in (3, 4) and raw:
                value = int.from_bytes(raw, endian)
            elif value_type == 5 and len(raw) >= 8:
                numerator = int.from_bytes(raw[:4], endian)
                denominator = int.from_bytes(raw[4:8], endian)
                value = numerator / denominator if denominator else None
        except Exception:
            value = None
        if value not in (None, ""):
            result[tag] = value
    return result


def jpeg_exif(path: str) -> dict:
    with open(path, "rb") as f:
        data = f.read(3_000_000)
    if data[:2] != b"\xff\xd8":
        return {}
    index = 2
    while index + 4 <= len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0xD9):
            index += 2
            continue
        if marker == 0xDA:
            break
        length = int.from_bytes(data[index + 2:index + 4], "big")
        if length < 2:
            break
        if marker == 0xE1 and data[index + 4:index + 10] == b"Exif\x00\x00":
            tiff = data[index + 10:index + 2 + length]
            if len(tiff) < 8:
                return {}
            if tiff[:2] == b"II":
                endian = "little"
            elif tiff[:2] == b"MM":
                endian = "big"
            else:
                return {}
            if int.from_bytes(tiff[2:4], endian) != 42:
                return {}
            ifd0 = int.from_bytes(tiff[4:8], endian)
            tags = read_tiff_ifd(tiff, ifd0, endian)
            exif_ptr = tags.get(0x8769)
            if isinstance(exif_ptr, int):
                for tag, value in read_tiff_ifd(tiff, exif_ptr, endian).items():
                    tags[("exif", tag)] = value
            return tags
        index += 2 + length
    return {}


def find_tesseract() -> str | None:
    """查找 tesseract 可执行文件；结果缓存。"""
    global _TESSERACT_CACHE
    if _TESSERACT_CACHE is False:
        return None
    if isinstance(_TESSERACT_CACHE, str):
        return _TESSERACT_CACHE
    for name in ("tesseract", "tesseract.exe"):
        found = shutil.which(name)
        if found:
            _TESSERACT_CACHE = found
            return found
    _TESSERACT_CACHE = False
    return None


def tesseract_langs(binary: str) -> tuple[str, list[str]]:
    notes = []
    try:
        proc = subprocess.run(
            [binary, "--list-langs"],
            capture_output=True,
            timeout=10,
        )
        listing = (proc.stdout or b"").decode("utf-8", errors="replace")
        listing += (proc.stderr or b"").decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        notes.append("未能列出 Tesseract 语言包，OCR 改用 eng")
        return "eng", notes
    if "chi_sim" in listing:
        return "chi_sim+eng", notes
    notes.append(
        "本机 Tesseract 未安装中文语言包 chi_sim，OCR 使用 eng。"
        "中文图片可能识别不准；语言包见 Tesseract 安装说明。"
    )
    return "eng", notes


def ocr_image(path: str, max_chars: int) -> tuple[str, list[str]]:
    binary = find_tesseract()
    if not binary:
        return "", [NOTE_OCR_MISSING_TESSERACT]
    lang, notes = tesseract_langs(binary)
    try:
        proc = subprocess.run(
            [binary, path, "stdout", "-l", lang, "--psm", "6"],
            capture_output=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return "", notes + ["未 OCR：Tesseract 识别超时"]
    except OSError as exc:
        return "", notes + [f"未 OCR：Tesseract 调用失败（{exc}）"]
    text = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode != 0 and not text:
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        return "", notes + [f"未 OCR：Tesseract 失败 {err or proc.returncode}"]
    notes = notes + [f"OCR 使用 Tesseract（{lang}）"]
    if not text:
        notes.append("OCR 未识别到文字")
    return text[:max_chars], notes


def extract_image(path: str, max_chars: int) -> tuple[str, dict, list[str]]:
    ext = os.path.splitext(path)[1].lower()
    meta, notes = {}, []
    if ext in {".jpg", ".jpeg"}:
        try:
            tags = jpeg_exif(path)
            mapping = {
                ("exif", 0x9003): "datetime_original",
                ("exif", 0x9004): "datetime_digitized",
                0x010F: "make",
                0x0110: "model",
                0x0132: "datetime",
            }
            for tag, name in mapping.items():
                if tag in tags:
                    meta[name] = tags[tag]
            if ("exif", 0xA002) in tags:
                meta["width"] = tags[("exif", 0xA002)]
            if ("exif", 0xA003) in tags:
                meta["height"] = tags[("exif", 0xA003)]
            if not meta:
                notes.append("未读取到 EXIF（可能被移除或格式不支持）")
        except Exception as exc:
            notes.append(f"EXIF 解析失败: {exc}")
    elif ext == ".png":
        try:
            with open(path, "rb") as f:
                header = f.read(24)
            if header[:8] == b"\x89PNG\r\n\x1a\n" and len(header) >= 24:
                meta["width"] = int.from_bytes(header[16:20], "big")
                meta["height"] = int.from_bytes(header[20:24], "big")
        except Exception as exc:
            notes.append(f"PNG 解析失败: {exc}")
    if ext in OCR_IMAGE_EXT:
        ocr_text, ocr_notes = ocr_image(path, max_chars)
        notes.extend(ocr_notes)
        text = ocr_text or (json.dumps(meta, ensure_ascii=False) if meta else "")
    else:
        notes.append(NOTE_OCR_UNSUPPORTED_IMAGE)
        text = json.dumps(meta, ensure_ascii=False) if meta else ""
    return text[:max_chars], meta, notes


# ---------------------------------------------------------------------------
# 其他
# ---------------------------------------------------------------------------

def extract_zip(path: str, max_chars: int) -> tuple[str, dict, list[str]]:
    notes = []
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if len(names) > 100:
                notes.append(f"压缩包共 {len(names)} 个条目，仅列出前 100 个")
            text = "\n".join(names[:100])
            return text[:max_chars], {"entries": len(names)}, notes
    except zipfile.BadZipFile:
        return "", {}, ["不是有效的 zip 文件（可能是 7z/rar 或已损坏）"]


def extract_one(path: str, max_chars: int) -> dict:
    result = {"path": path, "type": "other", "text": "", "meta": basic_meta(path), "notes": []}
    if not os.path.isfile(path):
        result["notes"].append("文件不存在或不是普通文件")
        return result
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in TEXT_EXT:
            result["type"] = "text"
            result["text"], extra = extract_plain(path, max_chars)
            result["notes"].extend(extra)
        elif ext == ".docx":
            result["type"] = "docx"
            result["text"], meta, notes = extract_docx(path, max_chars)
            result["meta"].update(meta)
            result["notes"].extend(notes)
        elif ext == ".pptx":
            result["type"] = "pptx"
            result["text"], meta, notes = extract_pptx(path, max_chars)
            result["meta"].update(meta)
            result["notes"].extend(notes)
        elif ext == ".xlsx":
            result["type"] = "xlsx"
            result["text"], meta, notes = extract_xlsx(path, max_chars)
            result["meta"].update(meta)
            result["notes"].extend(notes)
        elif ext == ".pdf":
            result["type"] = "pdf"
            result["text"], meta, notes = extract_pdf(path, max_chars)
            result["meta"].update(meta)
            result["notes"].extend(notes)
        elif ext in IMAGE_EXT:
            result["type"] = "image"
            result["text"], meta, notes = extract_image(path, max_chars)
            result["meta"].update(meta)
            result["notes"].extend(notes)
        elif ext == ".zip":
            result["type"] = "zip"
            result["text"], meta, notes = extract_zip(path, max_chars)
            result["meta"].update(meta)
            result["notes"].extend(notes)
        elif ext in OFFICE_OLD:
            result["type"] = "office-old"
            result["notes"].append("旧版 Office 二进制格式不支持抽文本，请按文件名/目录判断")
        elif ext in ARCHIVE_EXT:
            result["type"] = "archive"
            result["notes"].append("该压缩格式不支持读取内容清单，仅提供元信息")
        elif ext in MEDIA_EXT:
            result["type"] = "media"
            result["notes"].append("音视频文件不抽取文本")
        elif ext in {".exe", ".msi", ".msix", ".appx"}:
            result["type"] = "installer"
            result["notes"].append("安装包不抽取文本")
        else:
            result["notes"].append("未识别的文件类型，可结合文件名/目录判断")
    except Exception as exc:
        result["notes"].append(f"抽取失败: {type(exc).__name__}: {exc}")
    if len(result["text"]) >= max_chars:
        result["notes"].append(f"文本已截断到 {max_chars} 字符")
    return result


def main() -> int:
    setup_stdout()
    parser = argparse.ArgumentParser(description="多格式文本抽取（标准库；图片 OCR 需系统 tesseract）")
    parser.add_argument("files", nargs="+", help="一个或多个文件路径")
    parser.add_argument("--max-chars", type=int, default=4000, help="每个文件最多输出字符数")
    args = parser.parse_args()

    if args.max_chars < 1:
        print("错误: --max-chars 必须大于 0", file=sys.stderr)
        return 1

    results = [extract_one(path, args.max_chars) for path in args.files]
    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
