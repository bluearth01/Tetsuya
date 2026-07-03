#!/usr/bin/env python3
"""重ねるハザードマップ座標照会ツール.

住所をジオコーディングし、国交省「重ねるハザードマップ」配信タイルの
ピクセル値から各ハザードの該当/非該当を判定する。

使い方: python3 check_hazard.py "宇都宮市鐺山町2009"
"""
import io
import json
import math
import os
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request

try:
    from PIL import Image
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "pillow"], check=False)
    from PIL import Image

Z = 16  # 照会ズーム（1px ≒ 1.9m @北緯36度）
NEIGHBOR_TILES = 1  # 周辺3x3タイル（約1.7km四方）まで最寄り区域を探索

LAYERS = [
    ("洪水浸水想定（想定最大規模L2）", "01_flood_l2_shinsuishin_data"),
    ("洪水浸水想定（計画規模L1）", "01_flood_l1_shinsuishin_newlegend_data"),
    ("浸水継続時間", "01_flood_l2_keizoku_data"),
    ("家屋倒壊等氾濫想定区域（氾濫流）", "01_flood_l2_kaokutoukai_hanran_data"),
    ("家屋倒壊等氾濫想定区域（河岸侵食）", "01_flood_l2_kaokutoukai_kagan_data"),
    ("土砂災害警戒区域（土石流）", "05_dosekiryukeikaikuiki"),
    ("土砂災害警戒区域（急傾斜地）", "05_kyukeishakeikaikuiki"),
    ("土砂災害警戒区域（地すべり）", "05_jisuberikeikaikuiki"),
    ("津波浸水想定", "04_tsunami_newlegend_data"),
    ("高潮浸水想定", "03_hightide_l2_shinsuishin_data"),
]

FLOOD_DEPTH_LEGEND = {
    (247, 245, 169): "0.5m未満",
    (255, 216, 192): "0.5〜3.0m",
    (255, 183, 183): "3.0〜5.0m",
    (255, 145, 145): "5.0〜10.0m",
    (242, 133, 201): "10.0〜20.0m",
    (220, 122, 220): "20.0m以上",
}

DIRS = ["北", "北東", "東", "南東", "南", "南西", "西", "北西"]


def ssl_context():
    ca = os.environ.get("SSL_CERT_FILE") or "/root/.ccr/ca-bundle.crt"
    if os.path.exists(ca):
        return ssl.create_default_context(cafile=ca)
    return ssl.create_default_context()


CTX = ssl_context()


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, context=CTX, timeout=30).read()


def geocode(address):
    url = "https://msearch.gsi.go.jp/address-search/AddressSearch?q=" + urllib.parse.quote(address)
    results = json.loads(fetch(url))
    if not results:
        raise SystemExit(f"ジオコーディング失敗: {address}")
    hit = results[0]
    lon, lat = hit["geometry"]["coordinates"]
    return lon, lat, hit["properties"]["title"]


def elevation(lon, lat):
    url = f"https://cyberjapandata2.gsi.go.jp/general/dem/scripts/getelevation.php?lon={lon}&lat={lat}&outtype=JSON"
    try:
        data = json.loads(fetch(url))
        return f"{data['elevation']}m（{data.get('hsrc', '')}）"
    except Exception:
        return "取得失敗"


def check_layer(layer, lon, lat):
    """地点判定と、周辺タイル内の最寄り有色ピクセルを返す."""
    n = 2 ** Z
    xf = (lon + 180) / 360 * n
    yf = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
    xc, yc = int(xf), int(yf)
    gx, gy = xf * 256, yf * 256
    mpp = 40075016.686 * math.cos(math.radians(lat)) / (n * 256)

    point_color = None
    pts = []
    tiles_found = 0
    for tx in range(xc - NEIGHBOR_TILES, xc + NEIGHBOR_TILES + 1):
        for ty in range(yc - NEIGHBOR_TILES, yc + NEIGHBOR_TILES + 1):
            url = f"https://disaportaldata.gsi.go.jp/raster/{layer}/{Z}/{tx}/{ty}.png"
            try:
                img = Image.open(io.BytesIO(fetch(url))).convert("RGBA")
            except Exception:
                continue  # 404 = タイル範囲にデータ無し
            tiles_found += 1
            pix = img.load()
            for j in range(256):
                for i in range(256):
                    p = pix[i, j]
                    if p[3] > 0:
                        pts.append((tx * 256 + i, ty * 256 + j, p[:3]))
            if tx == xc and ty == yc:
                p = pix[int(gx) - xc * 256, int(gy) - yc * 256]
                if p[3] > 0:
                    point_color = p[:3]

    if point_color:
        depth = FLOOD_DEPTH_LEGEND.get(point_color, f"RGB{point_color}")
        return f"❌ 該当（区分: {depth}）"
    if not pts:
        area = (NEIGHBOR_TILES * 2 + 1) * 256 * mpp / 1000
        return f"✅ 非該当（周辺約{area:.1f}km四方にデータ無し）"
    best = min(pts, key=lambda p: (p[0] - gx) ** 2 + (p[1] - gy) ** 2)
    d = math.hypot(best[0] - gx, best[1] - gy) * mpp
    ang = (math.degrees(math.atan2(best[0] - gx, -(best[1] - gy))) + 360) % 360
    direction = DIRS[int((ang + 22.5) // 45) % 8]
    depth = FLOOD_DEPTH_LEGEND.get(best[2], f"RGB{best[2]}")
    return f"✅ 非該当（最寄り区域: {direction}約{d:.0f}m, 区分{depth}）"


def main():
    if len(sys.argv) < 2:
        raise SystemExit('使い方: python3 check_hazard.py "<住所>"')
    address = sys.argv[1]
    lon, lat, title = geocode(address)
    print(f"住所: {title}")
    print(f"座標: 東経{lon} / 北緯{lat}")
    print(f"標高: {elevation(lon, lat)}")
    print()
    ng = False
    for name, layer in LAYERS:
        result = check_layer(layer, lon, lat)
        ng = ng or result.startswith("❌")
        print(f"{name}: {result}")
    print()
    print(f"総合判定: {'❌ ハザード該当あり' if ng else '✅ 全ハザード非該当（判断指標クリア）'}")
    print(f"確認URL: https://disaportal.gsi.go.jp/maps/?ll={lat},{lon}&z=16")


if __name__ == "__main__":
    main()
