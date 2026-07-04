#!/usr/bin/env python3
"""
ハザードマップ判定スクリプト（クロコ / 物件調達課 ユフィ 標準ツール）

住所を国土地理院ジオコーダで緯度経度に変換し、
国土地理院「重ねるハザードマップ」のラスタタイルを地点ピクセルで判定する。

使い方:
    python3 hazard_check.py "栃木県宇都宮市鐺山町2009-5"
    python3 hazard_check.py --latlon 36.537579 139.974533

依存:
    pip install requests pillow
"""
import argparse
import math
import sys
from io import BytesIO

import requests
from PIL import Image

GEOCODE_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch"
UA = {"User-Agent": "Mozilla/5.0 (compatible; KurokoHazardCheck/1.0)"}

# レイヤー名: (タイルコード, 判定なしタイル404時の意味)
LAYERS = {
    "洪水浸水想定区域(想定最大規模 L2)": "01_flood_l2_shinsuishin_data",
    "洪水浸水想定区域(計画規模 L1)": "01_flood_l1_shinsuishin_newlegend_data",
    "土砂災害警戒区域(土石流)": "05_dosekiryukeikaikuiki",
    "土砂災害警戒区域(急傾斜地崩壊)": "04_kyukeishakeikaikuiki",
    "土砂災害警戒区域(地滑り)": "06_jisuberikeikaikuiki",
    "家屋倒壊等氾濫想定区域(氾濫流)": "01_flood_l2_kaokutoukai_hanranryuu_data",
    "家屋倒壊等氾濫想定区域(河岸侵食)": "01_flood_l2_kaokutoukai_kagansinshoku_data",
    "高潮浸水想定区域": "03_hightide_l2_shinsuishin_data",
    "津波浸水想定": "04_tsunami_newlegend_data",
    "ため池決壊": "10_tameike_data",
}

ZOOM_LEVELS = [17, 16, 15, 14]

# 重ねるハザードマップ 標準色 → 浸水深(目安)。 透明(alpha=0)は非該当。
FLOOD_COLOR_LEGEND = {
    (247, 245, 169, 255): "0.5m未満",
    (255, 216, 192, 255): "0.5〜3.0m",
    (255, 183, 183, 255): "3.0〜5.0m",
    (255, 145, 145, 255): "5.0〜10.0m",
    (255, 90, 90, 255): "10.0〜20.0m",
    (170, 0, 0, 255): "20.0m以上",
}


def geocode(address: str):
    r = requests.get(GEOCODE_URL, params={"q": address}, headers=UA, timeout=15)
    r.raise_for_status()
    results = r.json()
    if not results:
        raise ValueError(f"住所をジオコーディングできませんでした: {address}")
    lon, lat = results[0]["geometry"]["coordinates"]
    title = results[0]["properties"]["title"]
    return lat, lon, title


def latlon_to_tile(lat, lon, z):
    lat_rad = math.radians(lat)
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def pixel_in_tile(lat, lon, z, x, y):
    n = 2 ** z
    lat_rad = math.radians(lat)
    px = ((lon + 180.0) / 360.0 * n - x) * 256
    py = ((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n - y) * 256
    return int(px), int(py)


def describe_pixel(layer_name: str, rgba: tuple) -> str:
    if rgba[3] == 0:
        return "非該当"
    depth = FLOOD_COLOR_LEGEND.get(rgba)
    if depth:
        return f"該当（浸水深 目安: {depth}）"
    return f"該当（色コード: {rgba}）"


def check_layer(lat, lon, code):
    for z in ZOOM_LEVELS:
        x, y = latlon_to_tile(lat, lon, z)
        px, py = pixel_in_tile(lat, lon, z, x, y)
        url = f"https://disaportaldata.gsi.go.jp/raster/{code}/{z}/{x}/{y}.png"
        try:
            r = requests.get(url, headers=UA, timeout=15)
        except requests.RequestException as e:
            continue
        if r.status_code == 200:
            img = Image.open(BytesIO(r.content)).convert("RGBA")
            rgba = img.getpixel((px, py))
            return rgba, z
    return None, None


def run(address: str = None, latlon: tuple = None):
    if latlon:
        lat, lon = latlon
        title = f"{lat},{lon}"
    else:
        lat, lon, title = geocode(address)

    print(f"判定地点: {title}")
    print(f"緯度経度: {lat}, {lon}")
    print(f"重ねるハザードマップ: https://disaportal.gsi.go.jp/maps/index.html?ll={lat},{lon}&z=17")
    print()

    any_hit = False
    for name, code in LAYERS.items():
        rgba, z = check_layer(lat, lon, code)
        if rgba is None:
            print(f"✅ 非該当 | {name}（データなし）")
            continue
        verdict = describe_pixel(name, rgba)
        mark = "🔴" if rgba[3] != 0 else "✅"
        if rgba[3] != 0:
            any_hit = True
        print(f"{mark} {verdict} | {name}（z={z}, pixel={rgba}）")

    print()
    if any_hit:
        print("=> 社内基準「ハザード非該当」に抵触あり。立地判断は要注意（要重説確認）。")
    else:
        print("=> 全項目で非該当。社内基準「ハザード非該当」を満たす。")
    print("※ 地番の枝番までは厳密にジオコーディングされない場合あり。最終判断前に公式サイトで枝番指定の確認を推奨。")


def main():
    parser = argparse.ArgumentParser(description="国土地理院データによるハザード判定（クロコ標準ツール）")
    parser.add_argument("address", nargs="?", help="住所（例: 栃木県宇都宮市鐺山町2009-5）")
    parser.add_argument("--latlon", nargs=2, type=float, metavar=("LAT", "LON"), help="緯度経度を直接指定")
    args = parser.parse_args()

    if not args.address and not args.latlon:
        parser.error("住所または --latlon を指定してください")

    run(address=args.address, latlon=tuple(args.latlon) if args.latlon else None)


if __name__ == "__main__":
    main()
