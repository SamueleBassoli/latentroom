"""Image helpers (Pillow)."""

import io
import zlib
from pathlib import Path

from PIL import Image, ImageOps

# Invisible "made with AI" tag (EU AI Act art. 50(2)): the IPTC Digital Source Type, in XMP.
# Codes and labels: https://cv.iptc.org/newscodes/digitalsourcetype/
_IPTC = "http://cv.iptc.org/newscodes/digitalsourcetype/"
MADE = _IPTC + "trainedAlgorithmicMedia"  # IPTC: "Created using Generative AI"
EDITED = _IPTC + "compositeWithTrainedAlgorithmicMedia"  # IPTC: "Edited using Generative AI"
_XMP = (
    '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description rdf:about="" xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/"'
    ' Iptc4xmpExt:DigitalSourceType="{}"/>'
    '</rdf:RDF></x:xmpmeta><?xpacket end="r"?>'
)
_PNG_HEAD = 33  # signature (8) + IHDR chunk (4 length + 4 type + 13 data + 4 CRC)


def _upright(im: Image.Image) -> Image.Image:
    """Phone photos rotated by EXIF; RGBA when the picture has transparency, else RGB."""
    im = ImageOps.exif_transpose(im)
    alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
    return im.convert("RGBA" if alpha else "RGB")


def _flatten(im: Image.Image, background: tuple[int, int, int]) -> Image.Image:
    """Upright RGB: transparent pixels get `background`."""
    im = _upright(im)
    if im.mode == "RGBA":
        base = Image.new("RGB", im.size, background)
        base.paste(im, mask=im.getchannel("A"))
        return base
    return im


def _write_clean(im: Image.Image, dst: Path) -> None:
    """Pixels only: a fresh image has no info dict, so no text/exif/icc chunk is written."""
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
    Image.frombytes(im.mode, im.size, im.tobytes()).save(dst, "PNG")


def save_clean_png(src: Path, dst: Path) -> None:
    """Re-save pixels only: no text/metadata chunks (sd-cli writes the prompt into its PNGs)."""
    with Image.open(src) as im:
        _write_clean(im, dst)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")


def source_type(sources: list[str]) -> str:
    """MADE when there is no input picture, or every input carries a "created by AI" tag (a
    Latentroom picture, say); EDITED when any input is something else: the user's photo."""
    for src in sources:
        try:
            with Image.open(src) as im:
                if MADE.encode() not in im.info.get("xmp", b""):
                    return EDITED
        except OSError:  # gone or unreadable now: "edited" still says AI was used
            return EDITED
    return MADE


def mark_ai(png: Path, sources: list[str]) -> None:
    """Add the invisible "made with AI" tag to a PNG from _write_clean: the IPTC Digital Source
    Type in an XMP packet + Software=Latentroom. Nothing else: no prompt, seed, date or path.
    `sources`: the input pictures ("" ignored). Pixel data is not touched: no re-encode."""
    data = png.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError(f"{png.name} is not a PNG")
    xmp = _XMP.format(source_type([s for s in sources if s])).encode()
    tag = _chunk(b"iTXt", b"XML:com.adobe.xmp\0\0\0\0\0" + xmp)  # no compression, no language
    tag += _chunk(b"tEXt", b"Software\0Latentroom")
    png.write_bytes(data[:_PNG_HEAD] + tag + data[_PNG_HEAD:])  # before the pixels: read first


def thumbnail(src: Path, max_side: int = 384) -> tuple[bytes, str]:
    """Small preview for the contact sheet: (bytes, mime type)."""
    with Image.open(src) as im:
        small = _flatten(im, (255, 255, 255))
    small.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=85)
    return buf.getvalue(), "image/jpeg"


def image_size(src: Path) -> tuple[int, int]:
    """(width, height) as the user sees it, EXIF rotation applied."""
    with Image.open(src) as im:
        w, h = im.size
        if im.getexif().get(0x0112) in (5, 6, 7, 8):  # orientations that swap the sides
            w, h = h, w
    return w, h


def one_colour(src: Path) -> bool:
    """True when every pixel of `src` is the same colour: an all-black engine output, never a
    dark picture. getextrema() runs in C: (min, max) per channel."""
    with Image.open(src) as im:
        extrema = im.getextrema()
    if not isinstance(extrema[0], tuple):  # a one-channel image gives (min, max) itself
        extrema = (extrema,)
    return all(lo == hi for lo, hi in extrema)


def prepare_ref(
    src: Path, dst: Path, max_side: int = 1024, size: tuple[int, int] | None = None
) -> None:
    """Reference/init image -> RGB PNG, long side capped, so the engine gets a safe input.
    `size`: resize to exactly this (width, height) instead."""
    with Image.open(src) as im:
        out = _flatten(im, (255, 255, 255))
    if size is None and max(out.size) > max_side:
        scale = max_side / max(out.size)
        size = (max(1, round(out.width * scale)), max(1, round(out.height * scale)))
    if size is not None and size != out.size:
        out = out.resize(size, Image.Resampling.LANCZOS)
    _write_clean(out, dst)


def _mask(src: Path, size: tuple[int, int]) -> Image.Image:
    """Hard mask at `size`: white = repaint, black (or transparent) = keep."""
    with Image.open(src) as im:
        m = _flatten(im, (0, 0, 0)).convert("L")
    if m.size != size:
        m = m.resize(size, Image.Resampling.NEAREST)
    return m.point(lambda v: 255 if v >= 128 else 0)


def prepare_mask(src: Path, dst: Path, size: tuple[int, int]) -> None:
    """Inpaint mask for the engine, black/white at the job size."""
    _mask(src, size).save(dst, "PNG")


def paste_back(original: Path, generated: Path, mask: Path, dst: Path) -> None:
    """Inpaint: copy only masked pixels from `generated`; unmasked pixels stay byte-identical
    (alpha too: sd-cli's own PNGs are RGBA with alpha 253..255, measured)."""
    with Image.open(original) as im:
        orig = _upright(im)
    with Image.open(generated) as im:
        gen = im.convert(orig.mode)
    if gen.size != orig.size:
        gen = gen.resize(orig.size, Image.Resampling.LANCZOS)
    _write_clean(Image.composite(gen, orig, _mask(mask, orig.size)), dst)
