"""Pure image reshaping helpers shared by the CLI and the web curator.

Kept free of app/cli imports so both layers can use it without an import cycle.
The snap is a component-row engine, so these all produce a single horizontal row.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageChops, ImageDraw

CHROMA = (0, 255, 0)  # #00FF00 — the snap keys alpha off this exact background


def _runs(values: list[int], threshold: int) -> list[tuple[int, int]]:
    """Contiguous [start, end) ranges where a projection exceeds `threshold`."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(values):
        if value > threshold:
            if start is None:
                start = index
        elif start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(values)))
    return runs


def frames_from_sheet(data: bytes, *, bg_tolerance: int = 40, min_cell_frac: float = 0.02) -> list[bytes]:
    """Auto-detect sprite frames on a sheet by background gaps (no cols/rows needed).

    Samples the background from the top-left corner, masks off everything close to
    it, then splits into content row-bands and, within each, column-segments — so
    outer margins, uneven spacing, and empty rows are skipped automatically. Each
    frame is cropped to its true content box; returned in reading order.
    """
    with Image.open(BytesIO(data)) as opened:
        img = opened.convert("RGB")
    width, height = img.size
    background = img.getpixel((0, 0))
    diff = ImageChops.difference(img, Image.new("RGB", (width, height), background)).convert("L")
    mask = diff.point(lambda pixel: 255 if pixel > bg_tolerance else 0)

    min_h = max(1, int(height * min_cell_frac))
    min_w = max(1, int(width * min_cell_frac))
    row_profile = list(mask.resize((1, height), Image.BOX).getdata())
    frames: list[bytes] = []
    for y0, y1 in _runs(row_profile, 2):
        if y1 - y0 < min_h:
            continue
        band = mask.crop((0, y0, width, y1))
        col_profile = list(band.resize((width, 1), Image.BOX).getdata())
        for x0, x1 in _runs(col_profile, 2):
            if x1 - x0 < min_w:
                continue
            box = mask.crop((x0, y0, x1, y1)).getbbox()
            if box is None:
                continue
            crop = img.crop((x0 + box[0], y0 + box[1], x0 + box[2], y0 + box[3]))
            buffer = BytesIO()
            crop.save(buffer, format="PNG")
            frames.append(buffer.getvalue())
    return frames


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


def _load_rgba(data: bytes) -> Image.Image:
    with Image.open(BytesIO(data)) as opened:
        return opened.convert("RGBA")


def _png(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _even(value: int) -> int:
    return value & ~1


def normalize_frames(pixels: list[bytes], plains: list[bytes]) -> tuple[list[bytes], list[bytes]]:
    """Auto scale + recenter canonical 256x256 frames without breaking the contract.

    One shared integer nearest-neighbour factor (so relative frame sizes never
    wobble), each frame's content horizontally centred on x=128 (snapped to the
    2px logical grid) and its bottom row locked to y=255 (the foot anchor).
    The plain display twin gets the same geometry so the pixel-perfect toggle
    stays aligned.
    """
    images = [_load_rgba(data) for data in pixels]
    boxes = [image.getchannel("A").getbbox() for image in images]
    sizes = [(box[2] - box[0], box[3] - box[1]) for box in boxes if box is not None]
    if not sizes:
        return pixels, plains
    factor = max(1, min(256 // max(w for w, _ in sizes), 256 // max(h for _, h in sizes)))

    out_pixels: list[bytes] = []
    out_plains: list[bytes] = []
    for index, (image, box) in enumerate(zip(images, boxes)):
        plain = _load_rgba(plains[index]) if index < len(plains) else None
        if box is None:
            out_pixels.append(pixels[index])
            if index < len(plains):
                out_plains.append(plains[index])
            continue
        width, height = box[2] - box[0], box[3] - box[1]
        offset_x = _even(128 - (factor * width) // 2)
        offset_y = 256 - factor * height
        for source, sink in ((image, out_pixels), (plain, out_plains)):
            if source is None:
                continue
            content = source.crop(box).resize((factor * width, factor * height), Image.NEAREST)
            canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
            canvas.paste(content, (offset_x, offset_y))
            sink.append(_png(canvas))
    return out_pixels, out_plains


def nudge_frame(pixel: bytes, plain: bytes | None, dx: int) -> tuple[bytes, bytes | None]:
    """Shift one frame's content horizontally by an even pixel amount, clamped in-bounds.

    Vertical nudges are refused upstream: the canonical contract locks the
    content's bottom row to the cell bottom (foot anchor).
    """
    dx = _even(dx)
    image = _load_rgba(pixel)
    box = image.getchannel("A").getbbox()
    if box is None or dx == 0:
        return pixel, plain
    dx = max(-box[0], min(256 - box[2], dx))
    results: list[bytes | None] = []
    for data in (pixel, plain):
        if data is None:
            results.append(None)
            continue
        source = _load_rgba(data)
        canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        canvas.paste(source.crop(box), (box[0] + dx, box[1]))
        results.append(_png(canvas))
    return results[0], results[1]


def scale_nearest(data: bytes, factor: int) -> bytes:
    """Integer nearest-neighbour upscale of one RGBA frame (pixel-perfect)."""
    if factor <= 1:
        return data
    image = _load_rgba(data)
    return _png(image.resize((image.width * factor, image.height * factor), Image.NEAREST))


def frames_to_gif(frames: list[bytes], *, fps: int, loop: bool, factor: int = 1) -> bytes:
    """Assemble RGBA frames into an animated GIF with clean binary transparency."""
    if not frames:
        raise ValueError("no frames to export")
    duration = max(20, round(1000 / max(1, fps)))
    converted = []
    for data in frames:
        image = _load_rgba(scale_nearest(data, factor))
        # Binary alpha -> palette with one reserved transparent index.
        alpha = image.getchannel("A").point(lambda value: 255 if value < 128 else 0)
        quantized = image.convert("RGB").quantize(colors=255, method=Image.Quantize.FASTOCTREE)
        quantized.paste(255, mask=alpha)
        converted.append(quantized)
    buffer = BytesIO()
    converted[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=converted[1:],
        duration=duration,
        loop=0 if loop else 1,
        transparency=255,
        disposal=2,
        optimize=False,
    )
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
