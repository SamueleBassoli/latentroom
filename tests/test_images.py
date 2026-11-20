from pathlib import Path
from xml.etree import ElementTree

import pytest
from PIL import Image, PngImagePlugin

from latentroom.engine import images


def chunks(path: Path) -> list[bytes]:
    data = path.read_bytes()[8:]
    out = []
    while data:
        n = int.from_bytes(data[:4], "big")
        out.append(data[4:8])
        data = data[12 + n :]
    return out


def make(path: Path, size=(64, 48), color=(10, 200, 30), mode="RGB", **save) -> Path:
    Image.new(mode, size, color).save(path, **save)
    return path


def test_save_clean_png_strips_every_text_and_meta_chunk(tmp_path):
    info = PngImagePlugin.PngInfo()
    info.add_text("parameters", "a lighthouse at dusk, Steps: 20")
    info.add_itxt("prompt", "a lighthouse at dusk")
    src = make(tmp_path / "raw.png", pnginfo=info, dpi=(72, 72), exif=b"Exif\x00\x00MM")
    dst = tmp_path / "clean.png"
    images.save_clean_png(src, dst)
    assert set(chunks(dst)) == {b"IHDR", b"IDAT", b"IEND"}
    assert b"lighthouse" not in dst.read_bytes()
    with Image.open(src) as a, Image.open(dst) as b:
        assert a.tobytes() == b.tobytes()


def test_prepare_ref_rgb_long_side_and_exact_size(tmp_path):
    src = make(tmp_path / "big.png", (3000, 1500), (1, 2, 3, 0), mode="RGBA")
    images.prepare_ref(src, tmp_path / "r.png", max_side=1024)
    with Image.open(tmp_path / "r.png") as im:
        assert (im.mode, im.size) == ("RGB", (1024, 512))
        assert im.getpixel((0, 0)) == (255, 255, 255)  # transparent -> white, not black
    images.prepare_ref(src, tmp_path / "s.png", size=(992, 512))
    with Image.open(tmp_path / "s.png") as im:
        assert im.size == (992, 512)
    small = make(tmp_path / "small.jpg", (100, 80), format="JPEG")
    images.prepare_ref(small, tmp_path / "t.png")
    with Image.open(tmp_path / "t.png") as im:
        assert (im.mode, im.size) == ("RGB", (100, 80))


def test_exif_rotation_applied(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90: the phone held upright
    src = make(tmp_path / "phone.jpg", (200, 100), format="JPEG", exif=exif)
    assert images.image_size(src) == (100, 200)
    images.prepare_ref(src, tmp_path / "up.png")
    with Image.open(tmp_path / "up.png") as im:
        assert im.size == (100, 200)


def test_paste_back_keeps_unmasked_pixels_byte_identical(tmp_path):
    orig = Image.effect_noise((96, 64), 60).convert("RGB")  # noisy: any resampling would show
    orig.save(tmp_path / "orig.png")
    mask = Image.new("L", (96, 64), 0)
    mask.paste(255, (10, 10, 40, 30))
    mask.save(tmp_path / "mask.png")
    make(tmp_path / "gen.png", (64, 64), (250, 0, 0))  # engine size differs from the original
    images.paste_back(
        tmp_path / "orig.png", tmp_path / "gen.png", tmp_path / "mask.png", tmp_path / "out.png"
    )
    with Image.open(tmp_path / "out.png") as out:
        assert out.size == (96, 64)
        for y in range(64):
            for x in range(96):
                inside = 10 <= x < 40 and 10 <= y < 30
                if inside:
                    assert out.getpixel((x, y))[0] > 200
                else:
                    assert out.getpixel((x, y)) == orig.getpixel((x, y))
    assert set(chunks(tmp_path / "out.png")) == {b"IHDR", b"IDAT", b"IEND"}


def test_paste_back_keeps_alpha_of_an_rgba_original(tmp_path):
    # sd-cli writes RGBA with alpha 253..255, so every Latentroom picture is RGBA.
    # Flattening it over white changed the keep area by 1 level (15347/131072 pixels).
    orig = Image.effect_noise((32, 32), 60).convert("RGB")
    orig.putalpha(Image.effect_noise((32, 32), 60).point(lambda v: 253 + v % 3))
    orig.save(tmp_path / "orig.png")
    mask = Image.new("L", (32, 32), 0)
    mask.paste(255, (0, 0, 16, 32))
    mask.save(tmp_path / "mask.png")
    make(tmp_path / "gen.png", (32, 32), (250, 0, 0, 254), mode="RGBA")  # alpha kept too
    images.paste_back(
        tmp_path / "orig.png", tmp_path / "gen.png", tmp_path / "mask.png", tmp_path / "out.png"
    )
    with Image.open(tmp_path / "out.png") as out:
        assert out.mode == "RGBA"
        assert out.crop((16, 0, 32, 32)).tobytes() == orig.crop((16, 0, 32, 32)).tobytes()
        assert out.getpixel((0, 0)) == (250, 0, 0, 254)


def _palette_with_transparent_colour(path: Path) -> None:
    im = Image.new("P", (32, 32), 0)
    im.putpalette([0, 0, 0, 20, 90, 200])  # 0 = black, shown transparent; 1 = blue
    im.paste(1, (16, 0, 32, 32))
    im.save(path, transparency=0)


def _grey_with_alpha(path: Path) -> None:
    im = Image.new("LA", (32, 32), (0, 0))  # black, fully transparent
    im.paste((120, 255), (16, 0, 32, 32))
    im.save(path)


@pytest.mark.parametrize("make_orig", [_palette_with_transparent_colour, _grey_with_alpha])
def test_transparency_survives_in_every_png_flavour(tmp_path, make_orig):
    """Palette + transparent colour, grey + alpha: the clear half stays clear in
    the keep area of an inpaint, and turns white (not black) in a reference."""
    make_orig(tmp_path / "orig.png")
    Image.new("L", (32, 32), 0).save(tmp_path / "mask.png")  # keep everything
    make(tmp_path / "gen.png", (32, 32), (250, 0, 0))
    images.paste_back(
        tmp_path / "orig.png", tmp_path / "gen.png", tmp_path / "mask.png", tmp_path / "out.png"
    )
    images.prepare_ref(tmp_path / "orig.png", tmp_path / "ref.png")
    with Image.open(tmp_path / "out.png") as out, Image.open(tmp_path / "ref.png") as ref:
        assert out.mode == "RGBA" and out.getpixel((0, 0))[3] == 0
        assert out.getpixel((20, 0))[3] == 255
        assert ref.getpixel((0, 0)) == (255, 255, 255)


def test_prepare_mask_transparent_overlay_is_keep(tmp_path):
    overlay = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    overlay.paste((255, 255, 255, 255), (0, 0, 16, 16))
    overlay.save(tmp_path / "m.png")
    images.prepare_mask(tmp_path / "m.png", tmp_path / "mm.png", (32, 32))
    with Image.open(tmp_path / "mm.png") as m:
        assert m.size == (32, 32)
        assert m.getpixel((2, 2)) == 255 and m.getpixel((20, 20)) == 0


def test_thumbnail_is_small_jpeg(tmp_path):
    data, mime = images.thumbnail(make(tmp_path / "a.png", (1024, 512)), max_side=256)
    assert mime == "image/jpeg" and data[:2] == b"\xff\xd8"
    (tmp_path / "t.jpg").write_bytes(data)
    with Image.open(tmp_path / "t.jpg") as im:
        assert im.size == (256, 128)


def source_of(png: Path) -> str:
    """The XMP packet must be well-formed; returns its Digital Source Type."""
    with Image.open(png) as im:
        xmp = im.info["xmp"]
    desc = ElementTree.fromstring(xmp[xmp.index(b"<x:") : xmp.index(b"<?xpacket end")])[0][0]
    return desc.attrib["{http://iptc.org/std/Iptc4xmpExt/2008-02-29/}DigitalSourceType"]


def test_mark_ai_adds_only_the_tag_and_keeps_every_pixel_byte(tmp_path):
    raw = tmp_path / "raw.png"
    Image.effect_noise((64, 48), 60).convert("RGBA").save(raw)
    clean = tmp_path / "clean.png"
    images.save_clean_png(raw, clean)
    before = clean.read_bytes()
    images.mark_ai(clean, ["", ""])  # txt2img: no input picture
    assert chunks(clean) == [b"IHDR", b"iTXt", b"tEXt", b"IDAT", b"IEND"]
    after = clean.read_bytes()
    assert after.startswith(before[:33]) and after.endswith(before[33:])  # pixels not re-encoded
    with Image.open(clean) as im:
        im.verify()  # every chunk checksum
    with Image.open(clean) as im, Image.open(raw) as orig:
        assert im.info["Software"] == "Latentroom" and im.tobytes() == orig.tobytes()
    assert source_of(clean) == images.MADE


def test_source_type_photo_or_ai_picture(tmp_path):
    photo = make(tmp_path / "photo.jpg", format="JPEG")
    made = make(tmp_path / "made.png")
    images.mark_ai(made, [])
    other_ai = make(
        tmp_path / "ai.jpg", format="JPEG", xmp=images._XMP.format(images.MADE).encode()
    )
    assert images.source_type([str(made), str(other_ai)]) == images.MADE
    assert images.source_type([str(photo)]) == images.EDITED
    assert images.source_type([str(made), str(photo)]) == images.EDITED  # one photo is enough
    assert images.source_type([str(tmp_path / "gone.png")]) == images.EDITED
    edited = make(tmp_path / "edited.png")
    images.mark_ai(edited, [str(photo)])
    assert source_of(edited) == images.EDITED
    assert images.source_type([str(edited)]) == images.EDITED  # still has a photo in it
    with pytest.raises(ValueError):
        images.mark_ai(photo, [])


@pytest.mark.parametrize("mode, flat", [("RGB", (0, 0, 0)), ("L", 0), ("RGBA", (5, 5, 5, 255))])
def test_one_colour_is_exact_so_a_night_scene_passes(tmp_path, mode, flat):
    im = Image.new(mode, (64, 48), flat)
    im.save(tmp_path / "flat.png")
    im.putpixel((3, 3), 1 if mode == "L" else (1, 0, 0, 255)[: len(mode)])  # one star
    im.save(tmp_path / "night.png")
    assert images.one_colour(tmp_path / "flat.png")
    assert not images.one_colour(tmp_path / "night.png")
