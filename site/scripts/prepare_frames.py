"""Turn Blender's PNG frame sequences into web frames + manifests for the page.

    python scripts/prepare_frames.py [--quality 90]

Reads   public/liquid/frames/<anim>_<width>/f_0001.png ...
Writes  public/liquid/seq/<anim>_<width>/0000.webp ... + manifest.json
        public/liquid/seq/index.json   {"idle": "idle_1920", "story": "story_1920"} (largest of each)

The idle clip is made seamless: its last LOOP_BLEND frames are cross-faded into its first ones,
so frame N-1 flows straight back into frame 0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent / "public" / "liquid"
LOOP_BLEND = 24
QUALITY = int(sys.argv[sys.argv.index("--quality") + 1]) if "--quality" in sys.argv else 90


def frames_of(folder: Path) -> list[Path]:
    return sorted(p for p in folder.glob("f_*.png") if p.stat().st_size > 0)


def write_seq(name: str, images: list[Image.Image]) -> dict:
    out = ROOT / "seq" / name
    out.mkdir(parents=True, exist_ok=True)
    for i, im in enumerate(images):
        im.save(out / f"{i:04d}.webp", "WEBP", quality=QUALITY, method=5)
    w, h = images[0].size
    manifest = {"count": len(images), "ext": "webp", "width": w, "height": h}
    (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
    print(f"{name}: {len(images)} frames {w}x{h}")
    return manifest


def main() -> None:
    best: dict[str, tuple[int, str]] = {}
    for folder in sorted((ROOT / "frames").glob("*_*")):
        anim, _, width = folder.name.rpartition("_")
        files = frames_of(folder)
        if not files:
            continue
        images = [Image.open(p).convert("RGB") for p in files]
        if anim == "idle" and len(images) > 2 * LOOP_BLEND:
            body = images[LOOP_BLEND:]
            n = len(body)
            for k in range(n - LOOP_BLEND, n):  # tail fades into the head that was cut off
                alpha = (k - (n - LOOP_BLEND) + 1) / (LOOP_BLEND + 1)
                body[k] = Image.blend(body[k], images[k - (n - LOOP_BLEND)], alpha)
            images = body
        write_seq(folder.name, images)
        if int(width) > best.get(anim, (0, ""))[0]:
            best[anim] = (int(width), folder.name)
    index = {anim: name for anim, (_, name) in best.items()}
    (ROOT / "seq").mkdir(parents=True, exist_ok=True)
    (ROOT / "seq" / "index.json").write_text(json.dumps(index), encoding="utf8")
    print("index:", index)


if __name__ == "__main__":
    main()
