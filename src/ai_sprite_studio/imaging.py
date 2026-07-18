"""Pure image reshaping helpers shared by the CLI and the web curator.

Kept free of app/cli imports so both layers can use it without an import cycle.
The snap is a component-row engine, so these all produce a single horizontal row.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw

CHROMA = (0, 255, 0)  # #00FF00 — the snap keys alpha off this exact background


def prep_to_chroma(data: bytes, *, tolerance: int, pad: float) -> bytes:
    """Flood the border background to chroma green so the snap can key + center it.

    Only the background connected to the canvas edges is replaced, so interior
    same-colour pixels (a white shirt, the inside of a shape) survive.
    """
    with Image.open(BytesIO(data)) as opened:
        img = opened.convert("RGB")
    width, height = img.size
    for corner in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        ImageDraw.floodfill(img, corner, CHROMA, thresh=tolerance)
    if pad > 0:
        margin = round(max(width, height) * pad)
        side = max(width, height) + 2 * margin
        canvas = Image.new("RGB", (side, side), CHROMA)
        canvas.paste(img, ((side - width) // 2, (side - height) // 2))
        img = canvas
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def frames_to_row(frames: list[bytes]) -> bytes:
    """Lay independent frame images side by side into one 1xN row.

    Frames are normalised to the largest common cell (centred on chroma green), so
    per-frame sizes can differ; the snap re-centers each frame anyway.
    """
    images = []
    for data in frames:
        with Image.open(BytesIO(data)) as opened:
            images.append(opened.convert("RGB"))
    cell_w = max(image.width for image in images)
    cell_h = max(image.height for image in images)
    strip = Image.new("RGB", (cell_w * len(images), cell_h), CHROMA)
    for index, image in enumerate(images):
        offset_x = index * cell_w + (cell_w - image.width) // 2
        offset_y = (cell_h - image.height) // 2
        strip.paste(image, (offset_x, offset_y))
    buffer = BytesIO()
    strip.save(buffer, format="PNG")
    return buffer.getvalue()


def grid_to_row(data: bytes, *, cols: int, rows: int, frames: int) -> bytes:
    """Re-lay an NxM grid pose board into a single horizontal 1xN row.

    Cuts the first `frames` cells in reading order (left-to-right, top-to-bottom)
    and lines them up in one row the component-row snap reads correctly.
    """
    with Image.open(BytesIO(data)) as opened:
        img = opened.convert("RGB")
    width, height = img.size
    cell_w, cell_h = width // cols, height // rows
    strip = Image.new("RGB", (cell_w * frames, cell_h), CHROMA)
    for index in range(frames):
        row, col = divmod(index, cols)
        cell = img.crop((col * cell_w, row * cell_h, col * cell_w + cell_w, row * cell_h + cell_h))
        strip.paste(cell, (index * cell_w, 0))
    buffer = BytesIO()
    strip.save(buffer, format="PNG")
    return buffer.getvalue()
