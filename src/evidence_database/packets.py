"""Original-page HTML review packets. Creating a packet never approves evidence."""
from __future__ import annotations

import base64
import html
import json
from pathlib import Path


def create_packet(store, citation: str, outdir: Path) -> dict:
    import pymupdf

    evidence = store.get(citation)
    source = evidence["source"]
    block = evidence["block"]
    locator = block.get("locator", {})
    pages = sorted(set(locator.get("pages", []))
                   | ({locator["page"]} if locator.get("page") else set())
                   | {region["page"] for region in locator.get("regions", [])})
    if not pages:
        raise ValueError("This block has no original page locator; it cannot receive an original-page packet")
    outdir = Path(outdir)
    if outdir.exists():
        raise FileExistsError("A review packet requires a new directory; prior packets are never overwritten")
    doc = pymupdf.open(source["path"])
    try:
        if not doc.is_pdf or any(type(page) is not int or not 1 <= page <= len(doc) for page in pages):
            raise ValueError("Review packets require a PDF with valid physical page numbers")
        outdir.mkdir(parents=True, exist_ok=False)
        metadata = {"citation": citation, "source_id": source["source_id"],
                    "source_sha256": source["sha256"], "locator": locator,
                    "renderer": "pymupdf", "renderer_version": pymupdf.__version__,
                    "scale": 2, "rendered_pages": []}
        artifacts = {}
        figures = []
        for number in pages:
            page = doc[number - 1]
            logical = f"page-{number:04d}.png"
            png = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False, annots=True).tobytes("png")
            image_path = outdir / logical
            image_path.write_bytes(png)
            artifacts[logical] = image_path
            metadata["rendered_pages"].append({"page": number, "artifact": logical,
                                                "rotation": page.rotation,
                                                "width": page.rect.width, "height": page.rect.height})
            boxes = [r["bbox"] for r in locator.get("regions", []) if r["page"] == number]
            if locator.get("page") == number and locator.get("bbox") not in boxes and locator.get("bbox"):
                boxes.append(locator["bbox"])
            overlays = []
            for box in boxes:
                rectangle = pymupdf.Rect(box) * page.rotation_matrix
                style = (f"left:{100 * rectangle.x0 / page.rect.width:.6f}%;"
                         f"top:{100 * rectangle.y0 / page.rect.height:.6f}%;"
                         f"width:{100 * rectangle.width / page.rect.width:.6f}%;"
                         f"height:{100 * rectangle.height / page.rect.height:.6f}%")
                overlays.append(f'<span class="region" style="{style}" title="解析器定位区域"></span>')
            figures.append(f'<figure><figcaption>原 PDF 第 {number} 页（物理页码）</figcaption>'
                           f'<label><input type="checkbox" checked>显示解析器定位（尚需复核）</label>'
                           f'<div class="page"><img alt="Original page {number}" src="data:image/png;base64,'
                           f'{base64.b64encode(png).decode("ascii")}">{"".join(overlays)}</div></figure>')
    finally:
        doc.close()
    metadata_path = outdir / "packet.json"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    artifacts["packet.json"] = metadata_path
    title = html.escape(str(source.get("document_id", source["source_id"])))
    text = html.escape(block["text"])
    details = html.escape(json.dumps(locator, ensure_ascii=False, indent=2))
    diagnostics = html.escape(json.dumps({
        "conversion_status": evidence.get("conversion_status", "unknown"),
        "parser": evidence.get("parser", "unknown"),
        "parser_version": evidence.get("parser_version", "unknown"),
        "conversion_issues": evidence.get("conversion_issues", []),
        "block_issues": block.get("issues", []),
    }, ensure_ascii=False, indent=2))
    citation_html = html.escape(citation)
    content = f'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<title>原文复核 · {title}</title>
<style>
:root{{color-scheme:light;font-family:system-ui,"Microsoft YaHei",sans-serif;color:#18303b;background:#edf2f4}}
body{{margin:0}}header{{padding:24px 32px;background:#153f4a;color:white}}h1{{margin:0 0 10px;font-size:24px}}
main{{display:grid;grid-template-columns:minmax(300px,36%) 1fr;gap:20px;padding:20px;align-items:start}}
aside{{position:sticky;top:20px;background:white;padding:24px;border-radius:12px;max-height:85vh;overflow:auto}}
.status{{padding:12px;background:#fff1d8;border-left:4px solid #a46500;color:#593b0e}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.65 ui-monospace,monospace}}
.reading{{font:16px/1.75 system-ui,sans-serif}}code{{overflow-wrap:anywhere;font-size:12px}}
figure{{margin:0 0 20px;background:white;padding:12px;border-radius:12px}}img{{width:100%;height:auto}}
.page{{position:relative}}.page img{{display:block}}.region{{position:absolute;box-sizing:border-box;
border:2px solid #d14336;background:#ffcc0015;pointer-events:none}}
figure:has(input:not(:checked)) .region{{display:none}}label{{display:block;font-size:12px;padding:0 0 10px}}
figcaption{{font-size:14px;padding:6px 0 12px;color:#375763}}li{{margin:8px 0}}
@media(max-width:850px){{main{{grid-template-columns:1fr}}aside{{position:static;max-height:none}}}}
</style>
<header><h1>原文复核 · {title}</h1><div>对照原页，检查这条引文是否被准确读取</div></header>
<main><aside><p class="status">此页面不自动批准证据。复核必须针对具体引文单独记录；最新状态以数据库检查结果为准。</p>
<h2>转换文本</h2><pre class="reading">{text}</pre>
<h2>需要核对</h2><ul><li>数字、正负号、指数和单位是否一致。</li><li>数值是否属于正确的表格行列、实验组和条件。</li>
<li>引文是否完整，是否遗漏否定、限制条件或脚注。</li></ul>
<p>原页一致仅说明转换已复核，不等于科学结论成立。</p>
<details open><summary>解析状态与待检查问题</summary><pre>{diagnostics}</pre></details>
<details><summary>原文位置</summary><pre>{details}</pre></details>
<details><summary>引用与版本</summary><code>{citation_html}</code><p>原件 SHA256</p><code>{source['sha256']}</code></details>
<p>完成查看后，用 <code>evidence-db review</code> 记录具体引文、决定、复核者与理由。
本页为离线快照，不会自动修改数据库；packet-result.json 保存本次复核包编号。</p>
</aside><section>{''.join(figures)}</section></main></html>'''
    html_path = outdir / "index.html"
    html_path.write_text(content, encoding="utf-8")
    artifacts["index.html"] = html_path
    packet = store.register_packet(citation, artifacts=artifacts, metadata=metadata)
    result = {**packet, "html": str(html_path.resolve()), "directory": str(outdir.resolve()),
              "citation": citation, "approval_created": False}
    (outdir / "packet-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                                               encoding="utf-8")
    return result
