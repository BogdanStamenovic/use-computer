from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from use_computer.ocr import _parse_tsv, find_text, ocr
from use_computer.register import register, unregister

TSV = """level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext
5\t1\t1\t1\t1\t1\t100\t40\t60\t20\t95.0\tSave
5\t1\t1\t1\t1\t2\t170\t40\t40\t20\t90.0\tAs
5\t1\t2\t1\t1\t1\t400\t200\t80\t20\t88.0\tCancel
5\t1\t2\t1\t1\t2\t500\t200\t10\t20\t-1\t
"""


def test_parse_and_find_scales_boxes() -> None:
    lines = _parse_tsv(TSV, 0.5)
    assert [ln.text for ln in lines] == ["Save As", "Cancel"]
    hits = find_text(lines, "as")
    assert len(hits) == 1
    text, rect, _conf = hits[0]
    assert text == "Save As"
    assert (rect.x, rect.y, rect.w, rect.h) == (85.0, 20.0, 20.0, 10.0)
    assert find_text(lines, "save as")[0][1].w == 55.0


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_real_tesseract_reads_ui_text() -> None:
    img = Image.new("RGB", (420, 60), "white")
    font = None
    for cand in ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/noto/NotoSans-Regular.ttf",
                 "/usr/share/fonts/adwaita-sans-fonts/AdwaitaSans-Regular.ttf"):
        if Path(cand).exists():
            font = ImageFont.truetype(cand, 14)
            break
    ImageDraw.Draw(img).text((10, 20), "Enable notifications", fill="black",
                             font=font or ImageFont.load_default())
    lines = ocr(img)
    assert any("notifications" in ln.text.lower() for ln in lines), [ln.text for ln in lines]


def test_register_preserves_other_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / ".claude.json"
    cfg.write_text(json.dumps({"projects": {"/x": {"a": 1}}, "mcpServers": {"other": {"x": 1}}}))
    monkeypatch.setenv("USE_COMPUTER_CLAUDE_JSON", str(cfg))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert "registered" in register("/opt/uc/bin/use-computer-mcp")
    data = json.loads(cfg.read_text())
    assert data["projects"] == {"/x": {"a": 1}}
    assert data["mcpServers"]["other"] == {"x": 1}
    assert data["mcpServers"]["use-computer"]["command"] == "/opt/uc/bin/use-computer-mcp"
    assert "already registered" in register("/opt/uc/bin/use-computer-mcp")
    assert "removed" in unregister()
    assert "use-computer" not in json.loads(cfg.read_text())["mcpServers"]
    assert "was not in" in unregister()


def test_register_refuses_corrupt_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from use_computer import UseComputerError

    cfg = tmp_path / ".claude.json"
    cfg.write_text("{not json")
    monkeypatch.setenv("USE_COMPUTER_CLAUDE_JSON", str(cfg))
    with pytest.raises(UseComputerError):
        register("/x")
    assert cfg.read_text() == "{not json"
