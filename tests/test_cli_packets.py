"""User-visible CLI and original-page review packet regressions."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest


def cli(*args):
    return subprocess.run([sys.executable, "-m", "evidence_database.cli", *map(str, args)],
                          check=False, capture_output=True, text=True, encoding="utf-8")


def pdf_at(path, *, rotated=False):
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=300)
    page.insert_text((20, 40), "Dose 25 mg; group A has 20 samples.", fontsize=7)
    if rotated:
        page.set_rotation(90)
    doc.save(path)
    doc.close()


def sample(tmp_path, *, located=True, rotated=False):
    from evidence_database.store import Store
    path = tmp_path / "paper with spaces.pdf"
    pdf_at(path, rotated=rotated)
    store = Store(tmp_path / "db")
    src = store.ingest(path, "demo")
    conv = store.add_conversion(src["source_id"], parser="synthetic", parser_version="1",
                                blocks=[{"id": "b1", "kind": "text",
                                         "text": '<script>alert("x")</script> Dose 25 mg.',
                                         "locator": {"page": 1, "pages": [1],
                                                     "bbox": [20, 20, 180, 60]} if located else {}}])
    return store, conv["citations"][0]


def test_cli_help_is_available_without_database():
    result = cli("--help")
    assert result.returncode == 0, result.stderr
    assert "ingest" in result.stdout and "review" in result.stdout


def test_cli_pending_quote_has_nonzero_admission_exit(tmp_path):
    path = tmp_path / "paper with spaces.pdf"
    pdf_at(path)
    root = tmp_path / "db"
    result = cli("--db", root, "ingest", path, "--id", "demo")
    assert result.returncode == 0, result.stderr
    src = json.loads(result.stdout)
    result = cli("--db", root, "convert", "--source", src["source_id"], "--backend", "baseline")
    assert result.returncode == 0, result.stderr
    citation = json.loads(result.stdout)["citations"][0]
    result = cli("--db", root, "check", "--citation", citation, "--quote", "Dose 25 mg")
    assert result.returncode == 2, result.stderr
    data = json.loads(result.stdout)
    assert data["quote_matches"] is True and data["admitted"] is False


def test_cli_import_preserves_failure_and_reports_nonzero_exit(tmp_path):
    path = tmp_path / "paper.pdf"
    pdf_at(path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"documents": [{"id": "demo", "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "pages": 1}]}), encoding="utf-8")
    result = cli("--db", tmp_path / "db", "import-pilot", "--manifest", manifest,
                 "--parser", f"baseline={tmp_path / 'missing-parser'}")
    assert result.returncode == 2, result.stdout
    assert json.loads(result.stdout)["failed"] == 1


def test_packet_uses_original_png_and_escapes_untrusted_text(tmp_path):
    from evidence_database.packets import create_packet
    store, citation = sample(tmp_path)
    packet = create_packet(store, citation, tmp_path / "review")
    html = (tmp_path / "review" / "index.html").read_text(encoding="utf-8")
    assert '<script>alert("x")</script>' not in html
    assert "&lt;script&gt;" in html
    assert "data:image/png;base64," in html
    assert packet["packet_id"]
    original = pymupdf.open(store.get(citation)["source"]["path"])
    expected = original[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False, annots=True).tobytes("png")
    original.close()
    assert hashlib.sha256(expected).digest() == hashlib.sha256((tmp_path / "review" / "page-0001.png").read_bytes()).digest()
    assert store.check(citation, "Dose 25 mg")["admitted"] is False


def test_packet_handles_rotated_original_without_rewriting_it(tmp_path):
    from evidence_database.packets import create_packet
    store, citation = sample(tmp_path, rotated=True)
    source = store.get(citation)["source"]
    before = hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest()
    create_packet(store, citation, tmp_path / "review")
    image = pymupdf.Pixmap(str(tmp_path / "review" / "page-0001.png"))
    assert (image.width, image.height) == (600, 400)
    assert hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest() == before
    # On a 90-degree rotated 200x300 page, [20,20,180,60] becomes [240,20,280,180].
    html = (tmp_path / "review" / "index.html").read_text(encoding="utf-8")
    assert 'left:80.000000%;top:10.000000%;width:13.333333%;height:80.000000%' in html


def test_packet_refuses_existing_folder_and_missing_original_location(tmp_path):
    from evidence_database.packets import create_packet
    store, citation = sample(tmp_path, located=False)
    with pytest.raises(ValueError):
        create_packet(store, citation, tmp_path / "unlocated")
    existing = tmp_path / "exists"
    existing.mkdir()
    (existing / "keep.txt").write_text("keep")
    with pytest.raises((ValueError, FileExistsError)):
        create_packet(store, citation, existing)
    assert (existing / "keep.txt").read_text() == "keep"


def test_packet_renders_every_page_from_regions_only_locator(tmp_path):
    from evidence_database.packets import create_packet
    from evidence_database.store import Store
    path = tmp_path / "two-pages.pdf"
    pdf = pymupdf.open()
    pdf.new_page(width=200, height=300)
    pdf.new_page(width=200, height=300)
    pdf.save(path)
    pdf.close()
    store = Store(tmp_path / "db")
    source = store.ingest(path, "two-pages")
    conversion = store.add_conversion(source["source_id"], parser="synthetic", parser_version="1",
        blocks=[{"id": "b1", "kind": "text", "text": "Across pages", "locator": {"regions": [
            {"page": 1, "bbox": [20, 20, 100, 40]}, {"page": 2, "bbox": [20, 20, 100, 40]}]}}])
    packet = create_packet(store, conversion["citations"][0], tmp_path / "review")
    assert [p["page"] for p in packet["metadata"]["rendered_pages"]] == [1, 2]
    assert (tmp_path / "review" / "page-0002.png").exists()
