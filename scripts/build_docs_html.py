from __future__ import annotations

import argparse
import hashlib
import html
import re
import sys
from pathlib import Path

try:
    import markdown
except ModuleNotFoundError as exc:  # pragma: no cover - exercised by local setup failure
    raise SystemExit("缺少 markdown，请先执行 pip install -r requirements-dev.txt") from exc


ROOT = Path(__file__).resolve().parents[1]
ROOT_DOCUMENTS = (ROOT / "README.md", ROOT / "CONTRIBUTING.md")
DOCS_DIR = ROOT / "docs"
H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
MD_LINK_RE = re.compile(r'href="([^"#?]+)\.md(#[^"]*)?"')

STYLE = """
:root{--bg:#f5f7fb;--paper:#fff;--ink:#172033;--muted:#657087;--line:#dfe5ef;--brand:#3157d5;--soft:#eef2ff;--code:#152033;--shadow:0 12px 35px rgba(35,48,80,.09)}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}a{color:var(--brand);text-decoration:none}a:hover{text-decoration:underline}.layout{max-width:1480px;margin:0 auto;padding:28px 24px 64px;display:grid;grid-template-columns:285px minmax(0,1fr);gap:28px}.toc{position:sticky;top:20px;align-self:start;max-height:calc(100vh - 40px);overflow:auto;background:var(--paper);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);padding:18px 14px}.toc h2{font-size:16px;margin:0 8px 12px}.toc ul{padding-left:18px}.toc li{margin:4px 0}.toc a{color:#46536b;font-size:13px}.paper{min-width:0;background:var(--paper);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);padding:48px 56px}h1{margin:0 0 22px;font-size:34px;line-height:1.25}h2{scroll-margin-top:18px;margin:48px 0 18px;padding-top:10px;border-top:1px solid var(--line);font-size:24px;line-height:1.4}h3{margin:28px 0 10px;font-size:18px}p{margin:10px 0}blockquote{margin:18px 0;padding:14px 18px;border-left:4px solid var(--brand);background:var(--soft);color:#44506a;border-radius:0 9px 9px 0}ul,ol{padding-left:1.55em}li{margin:4px 0}code{font-family:"Cascadia Code",Consolas,monospace;font-size:.9em;background:#f0f3f8;border-radius:5px;padding:.12em .35em}pre{overflow:auto;background:var(--code);color:#e8edf7;border-radius:10px;padding:18px 20px;line-height:1.55}pre code{background:transparent;padding:0;color:inherit}table{width:100%;border-collapse:collapse;margin:18px 0;display:block;overflow-x:auto}th,td{padding:10px 12px;border:1px solid var(--line);text-align:left;vertical-align:top;min-width:110px}th{background:#f2f5fa}.meta{color:var(--muted);font-size:13px;margin-bottom:20px}.backtop{position:fixed;right:22px;bottom:22px;border:1px solid var(--line);background:#fff;padding:8px 12px;border-radius:999px;box-shadow:var(--shadow)}
@media(max-width:980px){.layout{display:block;padding:14px}.toc{position:static;max-height:none;margin-bottom:16px}.paper{padding:32px 26px}h1{font-size:28px}}@media(max-width:620px){body{font-size:14px}.layout{padding:8px}.paper{padding:25px 17px}.toc{display:none}h1{font-size:25px}h2{font-size:21px}.backtop{display:none}}@media print{body{background:#fff;font-size:11pt}.layout{display:block;max-width:none;padding:0}.toc,.backtop{display:none}.paper{border:0;box-shadow:none;padding:0}h2{break-after:avoid}pre,table,blockquote{break-inside:avoid}a{color:inherit}}
"""


def markdown_files() -> list[Path]:
    files = [path for path in ROOT_DOCUMENTS if path.exists()]
    files.extend(DOCS_DIR.rglob("*.md"))
    return sorted(files, key=lambda path: str(path).lower())


def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_document(source_path: Path) -> str:
    source = source_path.read_text(encoding="utf-8")
    title_match = H1_RE.search(source)
    title = title_match.group(1).strip() if title_match else source_path.stem
    renderer = markdown.Markdown(
        extensions=["extra", "sane_lists", "toc"],
        extension_configs={"toc": {"permalink": True, "toc_depth": "2-3"}},
        output_format="html5",
    )
    body = renderer.convert(source)
    body = MD_LINK_RE.sub(lambda match: f'href="{match.group(1)}.html{match.group(2) or ""}"', body)
    relative_source = source_path.relative_to(ROOT).as_posix()
    digest = source_hash(source)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><meta name="source-sha256" content="{digest}"><title>{html.escape(title)}</title><style>{STYLE}</style></head>
<body><div class="layout"><aside class="toc" aria-label="文档目录"><h2>目录</h2>{renderer.toc}</aside><main class="paper" id="top"><div class="meta">人类阅读版 · 来源：{html.escape(relative_source)} · 内容哈希：{digest[:12]}</div>{body}</main></div><a class="backtop" href="#top">返回顶部</a></body></html>
"""


def build() -> int:
    count = 0
    for source_path in markdown_files():
        output_path = source_path.with_suffix(".html")
        output_path.write_text(render_document(source_path), encoding="utf-8", newline="\n")
        count += 1
        print(f"生成 {output_path.relative_to(ROOT)}")
    print(f"完成：{count} 份 Markdown 均已生成 HTML")
    return 0


def check() -> int:
    failures: list[str] = []
    for source_path in markdown_files():
        output_path = source_path.with_suffix(".html")
        if not output_path.exists():
            failures.append(f"缺少 {output_path.relative_to(ROOT)}")
            continue
        source = source_path.read_text(encoding="utf-8")
        html_text = output_path.read_text(encoding="utf-8")
        expected = source_hash(source)
        match = re.search(r'<meta name="source-sha256" content="([0-9a-f]{64})">', html_text)
        if not match or match.group(1) != expected:
            failures.append(f"内容不同步 {output_path.relative_to(ROOT)}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"检查通过：{len(markdown_files())} 组 Markdown/HTML 内容同步")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="为项目正式 Markdown 文档生成同名离线 HTML")
    parser.add_argument("--check", action="store_true", help="只检查 HTML 是否存在且与 Markdown 同步")
    args = parser.parse_args()
    return check() if args.check else build()


if __name__ == "__main__":
    raise SystemExit(main())
