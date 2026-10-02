from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


PAGE_WIDTH, PAGE_HEIGHT = A4


def draw_image_page(pdf: canvas.Canvas, image_path: Path) -> None:
    with Image.open(image_path) as image:
        rgb_image = image.convert("RGB")
        image_width, image_height = rgb_image.size

        scale = min(PAGE_WIDTH / image_width, PAGE_HEIGHT / image_height)
        draw_width = image_width * scale
        draw_height = image_height * scale
        x = (PAGE_WIDTH - draw_width) / 2
        y = (PAGE_HEIGHT - draw_height) / 2

        pdf.drawImage(
            ImageReader(rgb_image),
            x,
            y,
            width=draw_width,
            height=draw_height,
            preserveAspectRatio=True,
            anchor="c",
        )
        pdf.showPage()


def render(input_dir: Path, output_path: Path) -> None:
    image_paths = sorted(input_dir.glob("*.png"))
    if not image_paths:
        raise FileNotFoundError(f"No PNG pages found in {input_dir}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(output_path), pagesize=A4, pageCompression=1)
    pdf.setTitle("MemoLens 项目说明书 图像版")
    pdf.setAuthor("OpenAI Codex")
    pdf.setSubject("MemoLens project book composed from image pages")

    for image_path in image_paths:
        draw_image_page(pdf, image_path)

    pdf.save()


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: render_project_image_pdf.py <image-page-dir> <output.pdf>", file=sys.stderr)
        return 1

    input_dir = Path(sys.argv[1]).resolve()
    output_path = Path(sys.argv[2]).resolve()

    if not input_dir.exists():
        print(f"Missing image page directory: {input_dir}", file=sys.stderr)
        return 1

    render(input_dir, output_path)
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
