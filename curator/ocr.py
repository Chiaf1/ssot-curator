"""Local OCR with Tesseract (option `images.reader: ocr`, recipe §10-11).

Tesseract is called directly (TSV output): no wrapper library. Every word comes with its box and its
confidence, so the redaction tool can black out words and the report can show doubtful readings.
Nothing is sent to the LLM here: the recognized text becomes an ordinary document."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


class OcrError(Exception):
    pass


@dataclass
class OcrWord:
    text: str
    conf: float              # 0-100 as given by Tesseract
    left: int
    top: int
    width: int
    height: int
    line: tuple[int, int, int]   # (block, paragraph, line)


@dataclass
class OcrResult:
    words: list[OcrWord] = field(default_factory=list)

    @property
    def lines(self) -> list[str]:
        out: dict[tuple, list[str]] = {}
        for w in self.words:
            out.setdefault(w.line, []).append(w.text)
        return [" ".join(ws) for _, ws in sorted(out.items())]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def mean_conf(self) -> float:
        return sum(w.conf for w in self.words) / len(self.words) if self.words else 0.0


def resolve_tesseract(command: str) -> str | None:
    if Path(command).is_file():
        return command
    return shutil.which(command)


def tesseract_info(command: str) -> tuple[str, list[str]]:
    """(version, installed languages); OcrError when the program cannot be run."""
    exe = resolve_tesseract(command)
    if not exe:
        raise OcrError(f"Tesseract not found: {command!r}")
    try:
        v = subprocess.run([exe, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=30)
        langs = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise OcrError(f"Tesseract cannot be run: {e}") from e
    version = (v.stdout or v.stderr).splitlines()[0].strip() if (v.stdout or v.stderr) else "unknown"
    out = (langs.stdout or "") + (langs.stderr or "")
    names = [line.strip() for line in out.splitlines()[1:] if line.strip() and " " not in line.strip()]
    return version, names


def parse_tsv(tsv: str) -> OcrResult:
    res = OcrResult()
    rows = tsv.splitlines()
    for row in rows[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5":           # level 5 = word
            continue
        text = cols[11].strip()
        if not text:
            continue
        res.words.append(OcrWord(text, float(cols[10]), int(cols[6]), int(cols[7]), int(cols[8]), int(cols[9]),
                                 (int(cols[2]), int(cols[3]), int(cols[4]))))
    return res


UPSCALE_BELOW = 1500       # pixels on the long side


class TesseractOcr:
    def __init__(self, command: str = "tesseract", languages: str = "eng", timeout: int = 120):
        self.command, self.languages, self.timeout = command, languages, timeout

    def read(self, image: Path | bytes, suffix: str = ".png") -> OcrResult:
        """OCR of an image file (or of image bytes). Small images (screenshots with small fonts) are
        enlarged 2x in grayscale first: measured on real screenshots, mean confidence 69 → 89.
        Word boxes are always in the coordinates of the original image."""
        import io
        from PIL import Image, UnidentifiedImageError
        exe = resolve_tesseract(self.command)
        if not exe:
            raise OcrError(f"Tesseract not found: {self.command!r} (set images.ocr.command in curator.yaml)")
        try:
            im = Image.open(image if isinstance(image, Path) else io.BytesIO(image))
            im.load()
        except (OSError, UnidentifiedImageError) as e:
            raise OcrError(f"unreadable image: {e}") from e
        factor = 2 if max(im.size) < UPSCALE_BELOW else 1
        if factor > 1:
            im = im.convert("L").resize((im.width * factor, im.height * factor), Image.LANCZOS)
        with tempfile.TemporaryDirectory(prefix="curator-ocr-") as tmp:
            path = Path(tmp) / "page.png"
            im.save(path, "PNG")
            try:
                p = subprocess.run([exe, str(path), "stdout", "-l", self.languages, "tsv"], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=self.timeout)
            except (OSError, subprocess.TimeoutExpired) as e:
                raise OcrError(f"Tesseract failed: {e}") from e
        if p.returncode != 0:
            raise OcrError(f"Tesseract failed ({p.returncode}): {p.stderr.strip()[:300]}")
        res = parse_tsv(p.stdout)
        for w in res.words:
            w.left, w.top, w.width, w.height = (w.left // factor, w.top // factor,
                                                -(-w.width // factor), -(-w.height // factor))
        return res


def image_reader(cfg) -> tuple["TesseractOcr | None", str]:
    """The reader of images for this configuration and a description of the mode (for warnings).
    `vision` needs a backend that can see images: none is available yet (claude-code would need an
    isolated system, recipe §7), so images stay unread and `doctor` says why."""
    if cfg.image_reader == "ocr":
        return TesseractOcr(cfg.ocr_command, cfg.ocr_languages, cfg.ocr_timeout), "ocr"
    if cfg.image_reader == "vision":
        return None, f"vision, not available with the {cfg.backend} backend yet"
    return None, "off"
