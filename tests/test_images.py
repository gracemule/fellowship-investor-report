"""Whatever image a person has reaches the model the way they see it: upright, opaque, readable, in any common format.

DeepSeek reads images natively; what these tests hold is everything before that: which files are accepted, and that a phone's
HEIC, a transparent screenshot, a rotated photo, a scan, or an animated GIF is not a reason for the model to see something else."""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image, ImageDraw

from chui_reporter import config
from chui_reporter.agent import ledger
from chui_reporter.extract import images
from chui_reporter.extract.images import IMAGE_EXT, ImageError, to_model_jpeg
from chui_reporter.workspace import sync


def decode(url: str) -> Image.Image:
    assert url.startswith("data:image/jpeg;base64,")
    im = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert im.format == "JPEG"
    return im.convert("RGB")


def text_image(size=(300, 120), bg="white", fg="black") -> Image.Image:
    im = Image.new("RGB", size, bg)
    ImageDraw.Draw(im).rectangle((20, 40, size[0] - 20, 70), fill=fg)        # a dark bar standing for text
    return im


def dark_pixels(im: Image.Image) -> int:
    return sum(im.convert("L").histogram()[:80])


# ---- every format the person might have ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("ext,fmt", [(".png", "PNG"), (".jpg", "JPEG"), (".webp", "WEBP"), (".gif", "GIF"), (".bmp", "BMP"),
                                     (".tiff", "TIFF"), (".avif", "AVIF")])
def test_each_common_format_reaches_the_model_as_the_same_readable_picture(tmp_path, ext, fmt):
    p = tmp_path / f"shot{ext}"
    text_image().save(p, fmt)
    out = decode(to_model_jpeg(p))
    assert out.size == (300, 120)
    assert dark_pixels(out) > 600, f"{fmt}: the dark bar is still there"
    assert out.getpixel((5, 5))[0] > 235, f"{fmt}: the page is still white"


def test_a_phones_heic_photo_is_read(tmp_path):
    heif = pytest.importorskip("pillow_heif")
    heif.register_heif_opener()
    p = tmp_path / "IMG_0001.HEIC"
    text_image().save(p, "HEIF")
    out = decode(to_model_jpeg(p))
    assert out.size == (300, 120) and dark_pixels(out) > 600
    assert ".heic" in IMAGE_EXT and ".heif" in IMAGE_EXT


# ---- the traps ------------------------------------------------------------------------------------------------------------------


def test_a_transparent_screenshot_is_put_on_white_so_dark_text_does_not_vanish(tmp_path):
    p = tmp_path / "transparent.png"
    im = Image.new("RGBA", (300, 120), (0, 0, 0, 0))                      # fully transparent page
    ImageDraw.Draw(im).rectangle((20, 40, 280, 70), fill=(0, 0, 0, 255))       # opaque black text
    im.save(p)
    out = decode(to_model_jpeg(p))
    assert out.getpixel((5, 5))[0] > 235, "the page behind the text is white, not black"
    assert dark_pixels(out) > 600 and dark_pixels(out) < 300 * 120 / 2, "the text is dark on a light page"


def test_a_palette_png_with_a_transparent_colour_is_put_on_white(tmp_path):
    p = tmp_path / "palette.png"
    im = Image.new("P", (200, 80), 0)
    im.putpalette([0, 0, 0] + [255, 255, 255] * 255)
    ImageDraw.Draw(im).rectangle((10, 30, 190, 50), fill=1)
    im.save(p, transparency=0)
    assert decode(to_model_jpeg(p)).getpixel((3, 3))[0] > 235


def test_a_photo_stored_sideways_is_turned_upright(tmp_path):
    p = tmp_path / "phone.jpg"
    im = Image.new("RGB", (200, 100), "red")
    ImageDraw.Draw(im).rectangle((100, 0, 200, 100), fill="blue")           # left red, right blue, as stored
    exif = Image.Exif()
    exif[0x0112] = 6                                                          # "rotate 90 degrees clockwise to view"
    im.save(p, exif=exif)
    out = decode(to_model_jpeg(p))
    assert out.size == (100, 200), "now portrait"
    top, bottom = out.getpixel((50, 10)), out.getpixel((50, 190))
    assert top[0] > 200 > top[2] and bottom[2] > 200 > bottom[0], "red is on top, blue below, as a person holds the phone"


def test_an_animated_gif_is_read_from_its_first_frame(tmp_path):
    p = tmp_path / "anim.gif"
    a, b = Image.new("RGB", (100, 60), "black"), Image.new("RGB", (100, 60), "white")
    a.save(p, save_all=True, append_images=[b], duration=100, loop=0)
    assert decode(to_model_jpeg(p)).getpixel((50, 30))[0] < 40


def test_a_16_bit_scan_is_scaled_not_clipped_to_white(tmp_path):
    p = tmp_path / "scan16.png"
    im = Image.new("I;16", (120, 60), 65535)
    ImageDraw.Draw(im).rectangle((10, 20, 110, 40), fill=0)
    im.save(p)
    out = decode(to_model_jpeg(p))
    assert out.getpixel((3, 3))[0] > 235 and dark_pixels(out) > 300, "white page, dark text (not an all-white or all-black picture)"


def test_a_cmyk_jpeg_is_converted(tmp_path):
    p = tmp_path / "print.jpg"
    Image.new("CMYK", (80, 40), (0, 0, 0, 0)).save(p, "JPEG")
    assert decode(to_model_jpeg(p)).getpixel((5, 5))[0] > 235


def test_a_large_image_is_reduced_to_what_the_model_needs(tmp_path):
    p = tmp_path / "big.png"
    text_image((5000, 3000)).save(p)
    out = decode(to_model_jpeg(p))
    assert max(out.size) == 1600 and out.size == (1600, 960), "same shape, smaller"
    assert max(decode(to_model_jpeg(p, max_side=800)).size) == 800


def test_a_photo_cut_short_by_a_bad_upload_is_still_read(tmp_path):
    p = tmp_path / "cut.jpg"
    buf = io.BytesIO()
    text_image((600, 400)).save(buf, "JPEG")
    p.write_bytes(buf.getvalue()[: int(len(buf.getvalue()) * 0.8)])
    out = decode(to_model_jpeg(p))
    assert out.size == (600, 400)


def test_bytes_work_as_well_as_paths():
    buf = io.BytesIO()
    text_image().save(buf, "PNG")
    assert decode(to_model_jpeg(buf.getvalue())).size == (300, 120)


def test_something_that_is_not_an_image_says_so_in_words(tmp_path):
    p = tmp_path / "notes.png"
    p.write_bytes(b"this is not a picture")
    with pytest.raises(ImageError, match="not an image this server can read"):
        to_model_jpeg(p)
    with pytest.raises(ImageError, match="not an image"):
        to_model_jpeg(b"junk")


# ---- the tool, and the rules around it ---------------------------------------------------------------------------------------


def test_the_look_at_image_tool_sends_the_normalised_picture_and_names_a_bad_file(tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage

    from chui_reporter.agent import llm, tools as T

    sent = []

    class Fake:
        def invoke(self, messages):
            sent.append(messages[0].content)
            return AIMessage(content="Total NAV 12,150,997.95")

    monkeypatch.setattr(llm, "get_llm", lambda *a, **k: Fake())
    monkeypatch.setattr(config, "SOURCE_ROOT", tmp_path)
    (tmp_path / "Uploads").mkdir()
    im = Image.new("RGBA", (300, 120), (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle((20, 40, 280, 70), fill=(0, 0, 0, 255))
    im.save(tmp_path / "Uploads" / "screenshot.png")
    (tmp_path / "Uploads" / "broken.tiff").write_bytes(b"not a tiff")
    (tmp_path / "Uploads" / "report.pdf").write_bytes(b"%PDF-1.4")

    out = T.look_at_image.invoke({"file_name": "screenshot", "question": "what is the NAV?"})
    assert out.startswith("[screenshot.png] Total NAV 12,150,997.95") and "cannot be verified by machine" in out
    blocks = sent[0]
    assert blocks[0] == {"type": "text", "text": "what is the NAV?"} and blocks[1]["type"] == "image_url"
    assert decode(blocks[1]["image_url"]["url"]).getpixel((3, 3))[0] > 235, "the model was given a white page, not a black one"
    bad = T.look_at_image.invoke({"file_name": "broken"})
    assert bad.startswith("ERROR:") and "broken.tiff" in bad and len(sent) == 1, "a file that cannot be read never reaches the model"
    assert T.look_at_image.invoke({"file_name": "report"}).startswith("ERROR:") and len(sent) == 1


def test_every_image_kind_can_be_attached_and_the_rules_are_one_list(store):
    assert sync.IMAGE_EXT is IMAGE_EXT and ledger._IMAGES is IMAGE_EXT and IMAGE_EXT <= sync.ATTACH_EXT
    for name in ("IMG_0001.HEIC", "scan.TIFF", "photo.jpg", "shot.avif", "old.bmp"):
        got = sync.put_attachment(store, name, b"x" + name.encode())
        assert got["kind"] == "image", name
    with pytest.raises(sync.SyncError, match="cannot be attached"):
        sync.put_attachment(store, "drawing.svg", b"<svg/>")
    assert images.IMAGE_EXT >= {".png", ".jpg", ".jpeg", ".webp", ".gif"}, "nothing that was accepted before is lost"
