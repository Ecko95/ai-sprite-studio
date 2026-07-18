"""Extract N evenly-spaced frames from a green-screen video for curator upload.

Usage: uv run python scripts/video_to_frames.py <video> <out_dir> [frames=12]

Needs ffmpeg on PATH. Each frame's background is snapped to exact chroma green
(global near-background replace) so the snap keys it cleanly; upload the
resulting PNGs as multiple files in the browser uploader.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile

from PIL import Image, ImageChops

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from ai_sprite_studio.imaging import CHROMA  # noqa: E402


def main() -> int:
	if len(sys.argv) < 3:
		print(__doc__)
		return 2
	video, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
	count = int(sys.argv[3]) if len(sys.argv) > 3 else 12
	out_dir.mkdir(parents=True, exist_ok=True)

	probe = subprocess.run(
		["ffprobe", "-v", "error", "-select_streams", "v", "-count_packets",
		 "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(video)],
		capture_output=True, text=True, check=True,
	)
	total = int(probe.stdout.strip())
	step = max(1, total // count)

	with tempfile.TemporaryDirectory() as staging:
		subprocess.run(
			["ffmpeg", "-y", "-v", "error", "-i", str(video),
			 "-vf", f"select=not(mod(n\\,{step}))", "-vsync", "0", f"{staging}/f%03d.png"],
			check=True,
		)
		for index, path in enumerate(sorted(Path(staging).glob("f*.png"))[:count]):
			image = Image.open(path).convert("RGB")
			background = image.getpixel((0, 0))
			distance = ImageChops.difference(image, Image.new("RGB", image.size, background)).convert("L")
			mask = distance.point(lambda value: 255 if value < 60 else 0)
			image.paste(CHROMA, mask=mask)
			image.save(out_dir / f"frame-{index:02d}.png")
	print(f"{min(count, total)} frames -> {out_dir}")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
