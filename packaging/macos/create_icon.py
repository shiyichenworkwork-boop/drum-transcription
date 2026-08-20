from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


def create_icon(destination: Path, size: int = 1024) -> None:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = round(size * 0.07)
    draw.rounded_rectangle(
        (margin, margin, size - margin, size - margin),
        radius=round(size * 0.23),
        fill="#181613",
    )
    bar_width = round(size * 0.075)
    centers = [0.23, 0.365, 0.5, 0.635, 0.77]
    heights = [0.25, 0.48, 0.68, 0.43, 0.22]
    for center, height in zip(centers, heights, strict=True):
        x = round(size * center)
        half_height = round(size * height / 2)
        draw.rounded_rectangle(
            (
                x - bar_width // 2,
                size // 2 - half_height,
                x + bar_width // 2,
                size // 2 + half_height,
            ),
            radius=bar_width // 2,
            fill="#ff7043",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG", optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    create_icon(args.destination)


if __name__ == "__main__":
    main()
