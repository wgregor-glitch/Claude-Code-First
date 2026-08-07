import csv
import io
import math
import time
import urllib.request
from PIL import Image, ImageDraw, ImageFont

CSV_PATH = "/root/.claude/uploads/5fca47e4-c527-52de-b9be-2f51cb4c5b55/81d7e2ea-straight_hormuz.csv"
OUT_PATH = "/tmp/claude-0/-home-user-Claude-Code-First/5fca47e4-c527-52de-b9be-2f51cb4c5b55/scratchpad/hormuz_map.png"

TILE_SIZE = 256
ZOOM = 9
USER_AGENT = "dataminr-internal-deck-map/1.0 (one-time internal analysis render)"

CIPHER_BLUE = (97, 135, 237)
SENTINEL_BLUE = (10, 23, 38)

# ---------------------------------------------------------------- load data

rows = []
with open(CSV_PATH, newline="") as f:
    r = csv.reader(f)
    next(r)
    for row in r:
        if not row or not row[0].strip():
            continue
        try:
            lat, lon = [float(v) for v in row[1].split(",")]
            alerts = int(row[2].replace(",", "").strip()) if row[2].strip() else 0
        except Exception:
            continue
        rows.append((lat, lon, alerts))

print("points:", len(rows))
lats = [r[0] for r in rows]
lons = [r[1] for r in rows]
lat_min, lat_max = min(lats), max(lats)
lon_min, lon_max = min(lons), max(lons)

pad_lat = (lat_max - lat_min) * 0.12
pad_lon = (lon_max - lon_min) * 0.12
lat_min, lat_max = lat_min - pad_lat, lat_max + pad_lat
lon_min, lon_max = lon_min - pad_lon, lon_max + pad_lon
print("bbox:", lat_min, lat_max, lon_min, lon_max)

# ---------------------------------------------------------------- slippy map math

def lonlat_to_pixel(lon, lat, zoom):
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n * TILE_SIZE
    y = (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n * TILE_SIZE
    return x, y


px_min, py_max = lonlat_to_pixel(lon_min, lat_min, ZOOM)
px_max, py_min = lonlat_to_pixel(lon_max, lat_max, ZOOM)

tile_x_min = int(px_min // TILE_SIZE)
tile_x_max = int(px_max // TILE_SIZE)
tile_y_min = int(py_min // TILE_SIZE)
tile_y_max = int(py_max // TILE_SIZE)
n_tiles_x = tile_x_max - tile_x_min + 1
n_tiles_y = tile_y_max - tile_y_min + 1
print("tile grid:", n_tiles_x, "x", n_tiles_y, "=", n_tiles_x * n_tiles_y, "tiles")

canvas = Image.new("RGB", (n_tiles_x * TILE_SIZE, n_tiles_y * TILE_SIZE), (230, 230, 230))

for tx in range(tile_x_min, tile_x_max + 1):
    for ty in range(tile_y_min, tile_y_max + 1):
        url = f"https://tile.openstreetmap.org/{ZOOM}/{tx}/{ty}.png"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read()
                tile_im = Image.open(io.BytesIO(data)).convert("RGB")
                canvas.paste(tile_im, ((tx - tile_x_min) * TILE_SIZE, (ty - tile_y_min) * TILE_SIZE))
                break
            except Exception as e:
                print("retry", tx, ty, e)
                time.sleep(1)
        time.sleep(0.15)

canvas_origin_x = tile_x_min * TILE_SIZE
canvas_origin_y = tile_y_min * TILE_SIZE

# crop to the padded bbox precisely
crop_left = int(px_min - canvas_origin_x)
crop_right = int(px_max - canvas_origin_x)
crop_top = int(py_min - canvas_origin_y)
crop_bottom = int(py_max - canvas_origin_y)
canvas = canvas.crop((crop_left, crop_top, crop_right, crop_bottom))
print("cropped size:", canvas.size)

# ---------------------------------------------------------------- desaturate + brand tint the basemap

gray = canvas.convert("L").convert("RGB")
base = Image.blend(canvas, gray, 0.55)

W, H = base.size
overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
draw = ImageDraw.Draw(overlay, "RGBA")


def to_px(lon, lat):
    x, y = lonlat_to_pixel(lon, lat, ZOOM)
    return x - canvas_origin_x - crop_left, y - canvas_origin_y - crop_top


max_alert = max(r[2] for r in rows)


def radius_for(a):
    return 2.2 + 9.0 * math.sqrt(a / max_alert)


rows_sorted = sorted(rows, key=lambda r: r[2])  # draw small points first, big hotspots on top
for lat, lon, a in rows_sorted:
    x, y = to_px(lon, lat)
    r = radius_for(a)
    alpha = 70 if a == 1 else min(60 + a * 8, 235)
    draw.ellipse([x - r, y - r, x + r, y + r], fill=(*CIPHER_BLUE, alpha))

# outline the top hotspots
top = sorted(rows, key=lambda r: -r[2])[:6]
for lat, lon, a in top:
    x, y = to_px(lon, lat)
    r = radius_for(a)
    draw.ellipse([x - r, y - r, x + r, y + r], outline=(255, 255, 255, 230), width=2)

composited = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")

# ---------------------------------------------------------------- chrome: title bar, legend, attribution

PAD = 28
TITLE_H = 70
LEGEND_H = 46
final = Image.new("RGB", (W + PAD * 2, H + PAD * 2 + TITLE_H + LEGEND_H), (255, 255, 255))
final.paste(composited, (PAD, PAD + TITLE_H))

fdraw = ImageDraw.Draw(final)


def font(size, bold=False):
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


fdraw.text((PAD, 22), "Strait of Hormuz — chokepoint entry & course-deviation alerts", font=font(22, True), fill=SENTINEL_BLUE)
fdraw.text((PAD, 48), f"{len(rows):,} alert locations, weighted by total alerts per point", font=font(13), fill=(90, 96, 107))

legend_y = PAD + TITLE_H + H + 14
fdraw.text((PAD, legend_y), "Alert volume:", font=font(12, True), fill=(45, 50, 63))
lx = PAD + 100
for label, a in [("1", 1), ("10", 10), ("22 (max)", max_alert)]:
    r = radius_for(a)
    fdraw.ellipse([lx - r, legend_y + 8 - r, lx + r, legend_y + 8 + r], fill=(*CIPHER_BLUE, 200))
    fdraw.text((lx + 16, legend_y + 2), label, font=font(12), fill=(45, 50, 63))
    lx += 90

attrib = "Map data (c) OpenStreetMap contributors"
fdraw.text((final.width - PAD - fdraw.textlength(attrib, font=font(10)), legend_y + 4), attrib, font=font(10), fill=(140, 145, 155))

final.save(OUT_PATH)
print("saved", OUT_PATH, final.size)
