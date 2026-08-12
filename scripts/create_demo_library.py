from __future__ import annotations

import argparse
import math
import random
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


WIDTH = 1440
HEIGHT = 960


@dataclass(frozen=True)
class Scene:
    filename: str
    captured_at: str
    sky_top: tuple[int, int, int]
    sky_bottom: tuple[int, int, int]
    ground: tuple[int, int, int]
    accent: tuple[int, int, int]
    motif: str


SCENES = (
    Scene("2022-04-18_quiet_mountain_sunrise.jpg", "2022:04:18 06:42:00", (46, 76, 112), (244, 183, 126), (41, 61, 53), (255, 220, 151), "mountain"),
    Scene("2022-04-19_blue_lake_cabin.jpg", "2022:04:19 08:15:00", (78, 132, 167), (207, 224, 218), (48, 91, 84), (206, 120, 70), "lake"),
    Scene("2022-09-03_forest_trail_mist.jpg", "2022:09:03 07:30:00", (111, 133, 126), (211, 215, 192), (42, 74, 55), (222, 200, 151), "forest"),
    Scene("2022-10-14_autumn_valley.jpg", "2022:10:14 16:12:00", (83, 115, 139), (225, 190, 145), (88, 87, 53), (190, 92, 48), "valley"),
    Scene("2023-02-11_snowy_peak.jpg", "2023:02:11 10:05:00", (75, 119, 158), (214, 229, 237), (85, 105, 112), (251, 244, 222), "snow"),
    Scene("2023-05-26_wildflower_meadow.jpg", "2023:05:26 17:08:00", (89, 145, 180), (225, 211, 170), (63, 114, 70), (240, 178, 91), "meadow"),
    Scene("2023-07-07_coastal_golden_hour.jpg", "2023:07:07 19:21:00", (73, 93, 137), (245, 168, 110), (40, 87, 102), (255, 209, 130), "coast"),
    Scene("2023-08-12_desert_dunes.jpg", "2023:08:12 18:02:00", (75, 112, 154), (236, 186, 137), (170, 103, 59), (248, 194, 112), "desert"),
    Scene("2024-01-28_city_rain_night.jpg", "2024:01:28 21:16:00", (17, 25, 48), (56, 69, 92), (20, 27, 39), (236, 151, 88), "city"),
    Scene("2024-03-09_red_rock_road.jpg", "2024:03:09 15:32:00", (75, 132, 168), (221, 188, 149), (130, 64, 46), (221, 175, 97), "road"),
    Scene("2024-06-21_ocean_cliff.jpg", "2024:06:21 18:44:00", (51, 105, 145), (181, 206, 204), (55, 77, 62), (238, 192, 114), "cliff"),
    Scene("2024-11-16_moonlit_beach.jpg", "2024:11:16 22:10:00", (12, 22, 48), (53, 70, 94), (24, 55, 70), (221, 226, 205), "moon"),
)


@dataclass(frozen=True)
class DemoVideo:
    filename: str
    scene_indexes: tuple[int, ...]
    width: int
    height: int
    with_audio: bool


DEMO_VIDEOS = (
    DemoVideo("demo_mountain_to_coast.mp4", (0, 2, 6), 1280, 720, True),
    DemoVideo("demo_vertical_city_story.mp4", (8, 9, 11), 720, 1280, False),
)


def mix(a: int, b: int, amount: float) -> int:
    return round(a + (b - a) * amount)


def gradient(image: Image.Image, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> None:
    draw = ImageDraw.Draw(image)
    for y in range(HEIGHT):
        amount = y / max(HEIGHT - 1, 1)
        color = tuple(mix(top[index], bottom[index], amount) for index in range(3))
        draw.line((0, y, WIDTH, y), fill=color)


def polygon_mountains(draw: ImageDraw.ImageDraw, scene: Scene, rng: random.Random) -> None:
    horizon = int(HEIGHT * 0.57)
    layers = (
        (horizon - 135, tuple(max(0, channel - 18) for channel in scene.sky_top)),
        (horizon - 55, tuple(max(0, channel - 16) for channel in scene.ground)),
        (horizon + 35, scene.ground),
    )
    for baseline, color in layers:
        points = [(0, HEIGHT)]
        x = 0
        while x <= WIDTH:
            peak = baseline - rng.randint(80, 260)
            points.extend([(x, baseline), (x + rng.randint(90, 180), peak)])
            x += rng.randint(190, 320)
        points.extend([(WIDTH, baseline), (WIDTH, HEIGHT)])
        draw.polygon(points, fill=color)


def render_scene(scene: Scene, index: int) -> Image.Image:
    rng = random.Random(4100 + index)
    image = Image.new("RGB", (WIDTH, HEIGHT))
    gradient(image, scene.sky_top, scene.sky_bottom)
    draw = ImageDraw.Draw(image, "RGBA")

    sun_x = int(WIDTH * (0.22 + (index % 5) * 0.13))
    sun_y = int(HEIGHT * (0.18 + (index % 3) * 0.055))
    sun_radius = 64 if scene.motif != "moon" else 48
    for radius in range(sun_radius * 3, sun_radius, -6):
        alpha = max(2, round(22 * (1 - radius / (sun_radius * 3))))
        draw.ellipse((sun_x - radius, sun_y - radius, sun_x + radius, sun_y + radius), fill=(*scene.accent, alpha))
    draw.ellipse((sun_x - sun_radius, sun_y - sun_radius, sun_x + sun_radius, sun_y + sun_radius), fill=(*scene.accent, 235))

    polygon_mountains(draw, scene, rng)
    horizon = int(HEIGHT * 0.59)

    if scene.motif in {"lake", "coast", "cliff", "moon"}:
        water = tuple(max(0, channel - 12) for channel in scene.sky_top)
        draw.rectangle((0, horizon, WIDTH, HEIGHT), fill=(*water, 255))
        for offset in range(0, HEIGHT - horizon, 22):
            alpha = max(8, 54 - offset // 10)
            draw.line((80 + offset, horizon + offset, WIDTH - 130, horizon + offset), fill=(*scene.accent, alpha), width=3)
    elif scene.motif == "desert":
        for row in range(4):
            y = horizon + row * 105
            points = [(0, HEIGHT), (0, y)]
            for x in range(0, WIDTH + 180, 180):
                points.append((x, y - int(54 * math.sin((x / 230) + row))))
            points.extend([(WIDTH, HEIGHT)])
            color = tuple(max(0, channel - row * 10) for channel in scene.ground)
            draw.polygon(points, fill=(*color, 255))
    else:
        draw.rectangle((0, horizon, WIDTH, HEIGHT), fill=(*scene.ground, 255))

    if scene.motif == "forest":
        for tree in range(34):
            x = rng.randint(-30, WIDTH + 30)
            base = rng.randint(horizon + 80, HEIGHT + 60)
            height = rng.randint(190, 460)
            shade = rng.randint(-16, 12)
            color = tuple(max(0, min(255, channel + shade)) for channel in scene.ground)
            draw.polygon(((x, base - height), (x - height // 5, base), (x + height // 5, base)), fill=(*color, 230))
        draw.polygon(((WIDTH * 0.43, HEIGHT), (WIDTH * 0.49, horizon), (WIDTH * 0.57, HEIGHT)), fill=(194, 169, 126, 210))
    elif scene.motif == "meadow":
        for _ in range(280):
            x = rng.randrange(WIDTH)
            y = rng.randrange(horizon + 80, HEIGHT)
            radius = rng.randint(2, 7)
            flower = rng.choice((scene.accent, (233, 218, 235), (237, 136, 107), (248, 228, 153)))
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(*flower, 210))
    elif scene.motif == "city":
        for building in range(18):
            x = building * 90 - 30
            height = rng.randint(180, 520)
            draw.rectangle((x, HEIGHT - height, x + rng.randint(70, 125), HEIGHT), fill=(12, 20, 35, 235))
            for wy in range(HEIGHT - height + 35, HEIGHT - 30, 55):
                for wx in range(x + 18, x + 78, 28):
                    if rng.random() > 0.42:
                        draw.rectangle((wx, wy, wx + 8, wy + 16), fill=(*scene.accent, rng.randint(90, 210)))
        for _ in range(85):
            x = rng.randrange(WIDTH)
            y = rng.randrange(HEIGHT)
            draw.line((x, y, x - 14, y + 42), fill=(210, 224, 236, 80), width=2)
    elif scene.motif == "road":
        draw.polygon(((WIDTH * 0.44, horizon), (WIDTH * 0.56, horizon), (WIDTH * 0.75, HEIGHT), (WIDTH * 0.25, HEIGHT)), fill=(44, 47, 47, 255))
        draw.polygon(((WIDTH * 0.495, horizon), (WIDTH * 0.505, horizon), (WIDTH * 0.535, HEIGHT), (WIDTH * 0.465, HEIGHT)), fill=(*scene.accent, 210))
    elif scene.motif == "lake":
        cabin_x, cabin_y = int(WIDTH * 0.68), horizon - 55
        draw.rectangle((cabin_x, cabin_y, cabin_x + 145, cabin_y + 100), fill=(99, 63, 43, 255))
        draw.polygon(((cabin_x - 15, cabin_y), (cabin_x + 72, cabin_y - 72), (cabin_x + 160, cabin_y)), fill=(*scene.accent, 255))
        draw.rectangle((cabin_x + 55, cabin_y + 32, cabin_x + 88, cabin_y + 100), fill=(47, 39, 34, 255))

    noise = Image.effect_noise((WIDTH, HEIGHT), 7).convert("L")
    noise_layer = Image.new("RGB", (WIDTH, HEIGHT), (246, 238, 223))
    image = Image.composite(noise_layer, image, noise.point(lambda value: min(34, value) // 2))
    return image.filter(ImageFilter.GaussianBlur(radius=0.35))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a privacy-safe synthetic library for MemoLens demos and QA.")
    parser.add_argument("--output", type=Path, default=Path("demo-photo-library"), help="Output directory (default: ./demo-photo-library).")
    parser.add_argument("--force", action="store_true", help="Overwrite generated files with matching names.")
    parser.add_argument("--photos-only", action="store_true", help="Skip the synthetic MP4 clips (FFmpeg is not required).")
    return parser.parse_args()


def create_demo_video(output: Path, video: DemoVideo, *, ffmpeg: str) -> None:
    seconds_per_scene = 3
    final_path = output / video.filename
    partial_path = output / f".{Path(video.filename).stem}.part.mp4"
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for scene_index in video.scene_indexes:
        command.extend(
            [
                "-loop",
                "1",
                "-t",
                str(seconds_per_scene),
                "-i",
                str(output / SCENES[scene_index].filename),
            ]
        )

    total_duration = seconds_per_scene * len(video.scene_indexes)
    if video.with_audio:
        command.extend(
            [
                "-f",
                "lavfi",
                "-t",
                str(total_duration),
                "-i",
                "sine=frequency=330:sample_rate=48000",
            ]
        )

    video_filters = []
    video_labels = []
    for input_index in range(len(video.scene_indexes)):
        label = f"v{input_index}"
        video_labels.append(f"[{label}]")
        video_filters.append(
            f"[{input_index}:v]"
            f"scale={video.width}:{video.height}:force_original_aspect_ratio=increase,"
            f"crop={video.width}:{video.height},fps=24,"
            f"trim=duration={seconds_per_scene},setpts=PTS-STARTPTS[{label}]"
        )
    video_filters.append(
        f"{''.join(video_labels)}concat=n={len(video_labels)}:v=1:a=0[outv]"
    )
    command.extend(["-filter_complex", ";".join(video_filters), "-map", "[outv]"])

    if video.with_audio:
        command.extend(
            [
                "-map",
                f"{len(video.scene_indexes)}:a:0",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-shortest",
            ]
        )
    command.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(partial_path),
        ]
    )
    try:
        subprocess.run(command, check=True, timeout=90)
        partial_path.replace(final_path)
    finally:
        partial_path.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)

    ffmpeg = None
    if not args.photos_only:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            print("FFmpeg was not found. Run `npm run setup:mac`, or retry with --photos-only.")
            return 3

    generated_paths = [output / scene.filename for scene in SCENES]
    if not args.photos_only:
        generated_paths.extend(output / video.filename for video in DEMO_VIDEOS)
    existing = [path for path in generated_paths if path.exists()]
    if existing and not args.force:
        print(f"Refusing to overwrite {len(existing)} existing demo image(s). Re-run with --force.")
        return 2

    for index, scene in enumerate(SCENES):
        image = render_scene(scene, index)
        exif = Image.Exif()
        exif[36867] = scene.captured_at
        exif[270] = f"MemoLens privacy-safe demo scene: {scene.filename}"
        image.save(output / scene.filename, format="JPEG", quality=91, optimize=True, exif=exif)

    video_count = 0
    if not args.photos_only:
        assert ffmpeg is not None
        for video in DEMO_VIDEOS:
            create_demo_video(output, video, ffmpeg=ffmpeg)
            video_count += 1

    print(f"Created {len(SCENES)} synthetic photos and {video_count} synthetic videos in {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
