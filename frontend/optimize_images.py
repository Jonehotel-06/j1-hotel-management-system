#!/usr/bin/env python3
# optimize_images.py
"""Re-encode the shipped hotel photographs so the same URL is smaller.

WHY IT EXISTS
    The public site shipped its hero and editorial photographs as JPEGs at
    150–320 KB each. The homepage hero is the largest contentful paint on the
    busiest page, so those bytes delay LCP directly.

WHAT IT DOES (deliberately conservative)
    * Rewrites the JPEG in place at its existing pixel dimensions — no file is
      renamed, moved or resized, so no HTML, CSS, JS, service-worker entry or
      admin-configured override has to change and nothing can break.
    * Optimised Huffman tables + progressive encoding + quality 80, which is
      visually indistinguishable from the originals at these sizes.
    * Skips any file that would not get at least 8% smaller.

    Deliberately NOT done here:
    * WebP/AVIF re-encoding. js/site-images.js swaps these same <img> elements
      to administrator-uploaded URLs at runtime, so a <picture>/<source> pair
      would keep showing the built-in asset and hide a configured hotel photo.
      Format conversion is better enabled at the edge (Cloudflare Polish, a
      dashboard setting) or done as a separate, tested change.

    The Open Graph image (assets/images/og-j-one-hotel-enugu.jpg) is generated
    from hero-1.jpg and is left alone here.

USAGE
    python3 optimize_images.py          # rewrite any image that gets >=8% smaller
    python3 optimize_images.py --check  # report sizes only, change nothing
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).parent
IMAGES = ROOT / "assets" / "images"
SKIP = {"og-j-one-hotel-enugu.jpg"}
QUALITY = 80
MIN_GAIN = 0.08


def reencode(path: Path) -> tuple[int, int]:
    before = path.stat().st_size
    with Image.open(path) as img:
        img = img.convert("RGB")
        buffer = io.BytesIO()
        img.save(
            buffer,
            format="JPEG",
            quality=QUALITY,
            optimize=True,
            progressive=True,
            subsampling="4:2:0",
        )
    data = buffer.getvalue()
    if len(data) >= before * (1 - MIN_GAIN):
        return before, before          # not worth it — keep the original bytes
    path.write_bytes(data)
    return before, len(data)


def main(argv: list[str]) -> int:
    check = "--check" in argv
    total_before = total_after = 0
    rows = []
    for path in sorted(IMAGES.glob("*.jpg")):
        if path.name in SKIP:
            continue
        before = path.stat().st_size
        if check:
            after = before
        else:
            before, after = reencode(path)
        total_before += before
        total_after += after
        rows.append((path.name, before, after))

    width = max(len(name) for name, _, _ in rows) if rows else 10
    for name, before, after in rows:
        saved = 100 * (before - after) / before if before else 0
        print(f"  {name:<{width}}  {before / 1024:7.1f} KB → {after / 1024:7.1f} KB  (−{saved:4.1f}%)")
    saved = 100 * (total_before - total_after) / total_before if total_before else 0
    verb = "would be" if check else "is"
    print(
        f"\n  {len(rows)} images: {total_before / 1024:.1f} KB → "
        f"{total_after / 1024:.1f} KB ({verb} {saved:.1f}% smaller)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
