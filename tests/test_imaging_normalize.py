"""Normalize/nudge keep the canonical contract: shared scale, centred, foot-locked."""

from io import BytesIO

from PIL import Image

from ai_sprite_studio.imaging import normalize_frames, nudge_frame


def _frame(box: tuple[int, int, int, int], color=(10, 20, 30, 255)) -> bytes:
	image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
	image.paste(Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), color), box[:2])
	buffer = BytesIO()
	image.save(buffer, format="PNG")
	return buffer.getvalue()


def _bbox(data: bytes) -> tuple[int, int, int, int]:
	with Image.open(BytesIO(data)) as opened:
		return opened.convert("RGBA").getchannel("A").getbbox()


def test_normalize_scales_centers_and_locks_baseline() -> None:
	# Two drifting frames: 32x32 at top-left, 40x24 floating mid-cell.
	pixels = [_frame((0, 0, 32, 32)), _frame((100, 60, 140, 84))]
	out_pixels, out_plains = normalize_frames(pixels, list(pixels))
	assert len(out_pixels) == 2 and len(out_plains) == 2
	# Shared factor: max content 40 wide, 32 tall -> 256//40 = 6.
	first, second = _bbox(out_pixels[0]), _bbox(out_pixels[1])
	assert first[2] - first[0] == 32 * 6 and second[2] - second[0] == 40 * 6
	for box in (first, second):
		assert box[3] == 256  # foot baseline locked to cell bottom
		center = (box[0] + box[2]) / 2
		assert abs(center - 128) <= 2  # horizontally centred on the 2px grid
		assert box[0] % 2 == 0  # stays on the logical grid


def test_nudge_moves_even_pixels_and_clamps() -> None:
	pixel = _frame((120, 200, 136, 256))
	moved, _ = nudge_frame(pixel, None, 5)  # odd snaps to 4
	assert _bbox(moved)[0] == 124
	clamped, _ = nudge_frame(pixel, None, 500)
	assert _bbox(clamped)[2] == 256
