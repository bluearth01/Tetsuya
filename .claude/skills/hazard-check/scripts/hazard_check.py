#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hazard_check.py — 住所からハザードマップを確認し、Excel評価表を出力する

データ出典:
  - ジオコーディング: 国土地理院 住所検索API
      https://msearch.gsi.go.jp/address-search/AddressSearch?q=<住所>
  - ハザード情報: ハザードマップポータルサイト「重ねるハザードマップ」配信タイル
      https://disaportaldata.gsi.go.jp/raster/<layer>/{z}/{x}/{y}.png

判定方法:
  指定住所の座標に対応するタイル画像のピクセル色を凡例色と照合して判定する。
  - 洪水/津波/高潮: 浸水深ランク（0.5m未満〜20m以上）を色から判定
  - 土砂災害(土石流/急傾斜地/地すべり): 色相から
      イエロー = 警戒区域 / レッド = 特別警戒区域 を判定
  地点ピクセルに加え、周辺約50m四方の最大値も算出する（境界誤差対策）。

使い方:
  python3 hazard_check.py "栃木県宇都宮市〇〇町1-2-3" [-o 出力先.xlsx] [--json]
  python3 hazard_check.py --demo   # ネット接続なしでサンプルExcelを生成

依存: pip install Pillow openpyxl
"""

import argparse
import datetime
import io
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request

GEOCODE_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch?q={q}"
TILE_URL = "https://disaportaldata.gsi.go.jp/raster/{layer}/{z}/{x}/{y}.png"
PORTAL_URL = "https://disaportal.gsi.go.jp/maps/?ll={lat},{lon}&z=16&base=pale"
USER_AGENT = "hazard-check-skill/1.0 (real-estate due diligence; personal use)"

ZOOM = 16          # 全レイヤー共通で配信されているズームレベル
TILE_SIZE = 256
NEIGHBOR_M = 50    # 周辺評価の半径（メートル）

# 種別: "depth"=浸水深凡例で判定 / "sediment"=イエロー・レッドで判定
LAYERS = [
    ("洪水浸水想定区域（想定最大規模）", "01_flood_l2_shinsuishin_data", "depth"),
    ("津波浸水想定", "04_tsunami_newlegend_data", "depth"),
    ("高潮浸水想定区域", "03_hightide_l2_shinsuishin_data", "depth"),
    ("土砂災害警戒区域（土石流）", "05_dosekiryukeikaikuiki", "sediment"),
    ("土砂災害警戒区域（急傾斜地の崩壊）", "05_kyukeishakeikaikuiki", "sediment"),
    ("土砂災害警戒区域（地すべり）", "05_jisuberikeikaikuiki", "sediment"),
]

# 浸水深の新凡例色（水防法改正後の共通凡例）: (RGB, ラベル, 評価, 深刻度ランク)
DEPTH_LEGEND = [
    ((247, 245, 169), "0.5m未満",      "△", 1),
    ((255, 216, 192), "0.5〜3.0m",     "×", 2),
    ((255, 183, 183), "3.0〜5.0m",     "×", 3),
    ((255, 145, 145), "5.0〜10.0m",    "×", 4),
    ((242, 133, 201), "10.0〜20.0m",   "×", 5),
    ((220, 122, 220), "20.0m以上",     "×", 6),
]
DEPTH_MATCH_MAX_DIST = 90.0  # 凡例色とのユークリッド距離の許容値


class Result:
    def __init__(self, hazard, spot, spot_eval, around, note=""):
        self.hazard = hazard        # ハザード種別名
        self.spot = spot            # 地点判定（浸水深 or イエロー/レッド or 該当なし）
        self.spot_eval = spot_eval  # ○ / △ / ×
        self.around = around        # 周辺約50mの最大判定
        self.note = note


# ---------------------------------------------------------------- 座標・タイル

def geocode(address):
    url = GEOCODE_URL.format(q=urllib.parse.quote(address))
    data = json.loads(_http_get(url).decode("utf-8"))
    if not data:
        raise SystemExit(f"住所が見つかりません: {address}")
    hit = data[0]
    lon, lat = hit["geometry"]["coordinates"]
    title = hit.get("properties", {}).get("title", address)
    return lat, lon, title


def _http_get(url, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last = e
        except Exception as e:  # ネットワーク断・タイムアウト等
            last = e
        time.sleep(2 ** i)
    raise last


def latlon_to_global_pixel(lat, lon, z):
    """緯度経度 → ズームzのグローバルピクセル座標（Webメルカトル）"""
    n = 2 ** z * TILE_SIZE
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def meters_per_pixel(lat, z):
    return 156543.03392 * math.cos(math.radians(lat)) / (2 ** z)


class TileSampler:
    """レイヤーのタイルを必要に応じて取得し、グローバルピクセル座標で色を返す"""

    def __init__(self, layer, z=ZOOM):
        self.layer = layer
        self.z = z
        self.cache = {}  # (tx, ty) -> PIL.Image (RGBA) or None(404)

    def _tile(self, tx, ty):
        key = (tx, ty)
        if key not in self.cache:
            from PIL import Image
            url = TILE_URL.format(layer=self.layer, z=self.z, x=tx, y=ty)
            try:
                raw = _http_get(url)
                self.cache[key] = Image.open(io.BytesIO(raw)).convert("RGBA")
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    self.cache[key] = None  # タイルなし＝区域指定なし
                else:
                    raise
        return self.cache[key]

    def pixel(self, gx, gy):
        """グローバルピクセル座標のRGBA。タイルが無ければ None"""
        tx, ty = int(gx) // TILE_SIZE, int(gy) // TILE_SIZE
        img = self._tile(tx, ty)
        if img is None:
            return None
        return img.getpixel((int(gx) % TILE_SIZE, int(gy) % TILE_SIZE))


# ---------------------------------------------------------------- 色 → 判定

def classify_depth(rgba):
    """浸水深凡例色に最近傍マッチ。(ラベル, 評価, ランク) / None"""
    if rgba is None or rgba[3] < 128:
        return None
    r, g, b = rgba[:3]
    best = None
    for (lr, lg, lb), label, ev, rank in DEPTH_LEGEND:
        d = math.dist((r, g, b), (lr, lg, lb))
        if d <= DEPTH_MATCH_MAX_DIST and (best is None or d < best[0]):
            best = (d, label, ev, rank)
    if best:
        return best[1], best[2], best[3]
    return ("浸水域（深さ区分不明・要目視確認）", "×", 2)


def classify_sediment(rgba):
    """土砂災害タイルの色相からイエロー/レッドを判定。(ラベル, 評価, ランク) / None"""
    if rgba is None or rgba[3] < 128:
        return None
    import colorsys
    r, g, b = [v / 255.0 for v in rgba[:3]]
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    hue = h * 360.0
    if s < 0.15:  # ほぼ無彩色（背景系）は非該当扱い
        return None
    if hue < 32 or hue >= 300:
        return ("特別警戒区域（レッド）", "×", 2)
    if 32 <= hue < 75:
        return ("警戒区域（イエロー）", "△", 1)
    return ("区域該当（区分不明色・要目視確認）", "△", 1)


def evaluate_layer(name, layer, kind, lat, lon):
    sampler = TileSampler(layer)
    gx, gy = latlon_to_global_pixel(lat, lon, ZOOM)
    classify = classify_depth if kind == "depth" else classify_sediment

    spot = classify(sampler.pixel(gx, gy))

    # 周辺約50m四方の最大判定（4px間引きで走査）
    radius_px = max(2, int(NEIGHBOR_M / meters_per_pixel(lat, ZOOM)))
    worst = None
    for dy in range(-radius_px, radius_px + 1, 4):
        for dx in range(-radius_px, radius_px + 1, 4):
            c = classify(sampler.pixel(gx + dx, gy + dy))
            if c and (worst is None or c[2] > worst[2]):
                worst = c

    if spot:
        return Result(name, spot[0], spot[1],
                      worst[0] if worst else spot[0])
    around = worst[0] if worst else "該当なし"
    note = "地点は非該当だが周辺約50m内に区域あり" if worst else ""
    return Result(name, "該当なし", "○", around, note)


# ---------------------------------------------------------------- Excel出力

FILL = {
    "○": "C6EFCE",  # 緑
    "△": "FFEB9C",  # 黄
    "×": "FFC7CE",  # 赤
}
FONT_COLOR = {"○": "006100", "△": "9C6500", "×": "9C0006"}


def write_excel(path, address, title, lat, lon, results, demo=False):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "ハザード評価"

    thin = Side(style="thin", color="999999")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    today = datetime.date.today().strftime("%Y/%m/%d")

    ws["A1"] = "ハザードマップ評価表"
    ws["A1"].font = Font(size=14, bold=True)
    meta = [
        ("対象住所", address),
        ("ジオコーディング結果", title),
        ("緯度・経度", f"{lat:.6f}, {lon:.6f}"),
        ("確認日", today),
        ("出典", "ハザードマップポータルサイト「重ねるハザードマップ」（国土交通省）"),
        ("確認用URL", PORTAL_URL.format(lat=lat, lon=lon)),
    ]
    if demo:
        meta.insert(0, ("注意", "★デモデータ（実際の判定ではありません）★"))
    row = 3
    for k, v in meta:
        ws.cell(row=row, column=1, value=k).font = Font(bold=True)
        ws.cell(row=row, column=2, value=v)
        row += 1

    row += 1
    headers = ["ハザード種別", "地点判定", "浸水深／区域区分", "周辺約50m最大", "評価", "備考"]
    for c, h in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=c, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="4472C4")
        cell.border = border
        cell.alignment = Alignment(horizontal="center", vertical="center")
    header_row = row

    worst_eval = "○"
    order = {"○": 0, "△": 1, "×": 2}
    for res in results:
        row += 1
        applicable = "該当" if res.spot != "該当なし" else "該当なし"
        detail = res.spot if res.spot != "該当なし" else "―"
        vals = [res.hazard, applicable, detail, res.around, res.spot_eval, res.note]
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=row, column=c, value=v)
            cell.border = border
            if c >= 2:
                cell.alignment = Alignment(horizontal="center", vertical="center",
                                           wrap_text=True)
        ev = ws.cell(row=row, column=5)
        ev.fill = PatternFill("solid", fgColor=FILL[res.spot_eval])
        ev.font = Font(bold=True, color=FONT_COLOR[res.spot_eval])
        if order[res.spot_eval] > order[worst_eval]:
            worst_eval = res.spot_eval

    # 総合判定（社内基準: 立地はハザード非該当であること）
    row += 2
    ws.cell(row=row, column=1, value="総合判定（社内基準: ハザード非該当）").font = Font(bold=True)
    verdict = {"○": "適合（全ハザード非該当）",
               "△": "要検討（軽微な該当あり）",
               "×": "不適合（重大な該当あり）"}[worst_eval]
    vc = ws.cell(row=row, column=2, value=f"{worst_eval} {verdict}")
    vc.fill = PatternFill("solid", fgColor=FILL[worst_eval])
    vc.font = Font(bold=True, color=FONT_COLOR[worst_eval])

    row += 2
    legend = [
        "【評価基準】○=非該当 / △=浸水深0.5m未満・土砂イエロー（警戒区域） / ×=浸水深0.5m以上・土砂レッド（特別警戒区域）",
        "【注意】本表はタイル画像の色判定による自動評価です。売買・融資判断の前に必ず「重ねるハザードマップ」および",
        "自治体公表のハザードマップ原本を目視で確認してください。地点判定は座標精度（住所ジオコーディング）に依存します。",
    ]
    for line in legend:
        ws.cell(row=row, column=1, value=line).font = Font(size=9, color="666666")
        row += 1

    widths = [34, 10, 24, 24, 7, 30]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

    wb.save(path)


# ---------------------------------------------------------------- メイン

def sanitize_for_filename(s):
    s = re.sub(r"[\\/:*?\"<>|\s]", "", s)
    return s[:30] or "対象地"


def demo_results():
    return [
        Result("洪水浸水想定区域（想定最大規模）", "0.5〜3.0m", "×", "3.0〜5.0m"),
        Result("津波浸水想定", "該当なし", "○", "該当なし"),
        Result("高潮浸水想定区域", "該当なし", "○", "該当なし"),
        Result("土砂災害警戒区域（土石流）", "警戒区域（イエロー）", "△", "特別警戒区域（レッド）",
               "周辺にレッド区域あり・要注意"),
        Result("土砂災害警戒区域（急傾斜地の崩壊）", "該当なし", "○", "警戒区域（イエロー）",
               "地点は非該当だが周辺約50m内に区域あり"),
        Result("土砂災害警戒区域（地すべり）", "該当なし", "○", "該当なし"),
    ]


def main():
    ap = argparse.ArgumentParser(description="住所からハザードマップ評価Excelを生成")
    ap.add_argument("address", nargs="?", help="対象住所（例: 栃木県宇都宮市〇〇町1-2-3）")
    ap.add_argument("-o", "--output", help="出力xlsxパス（省略時: 年月日_ハザード評価_住所.xlsx）")
    ap.add_argument("--json", action="store_true", help="判定結果をJSONでも標準出力")
    ap.add_argument("--demo", action="store_true", help="ネット接続なしでサンプルを生成")
    args = ap.parse_args()

    if args.demo:
        address, title, lat, lon = "（デモ）栃木県宇都宮市", "デモデータ", 36.55, 139.88
        results = demo_results()
    else:
        if not args.address:
            ap.error("住所を指定してください（または --demo）")
        address = args.address
        lat, lon, title = geocode(address)
        print(f"ジオコーディング: {title} → 緯度 {lat:.6f}, 経度 {lon:.6f}")
        results = []
        for name, layer, kind in LAYERS:
            print(f"確認中: {name} ...")
            results.append(evaluate_layer(name, layer, kind, lat, lon))

    out = args.output
    if not out:
        stamp = datetime.date.today().strftime("%Y%m%d")
        out = f"{stamp}_ハザード評価_{sanitize_for_filename(address)}.xlsx"
    write_excel(out, address, title, lat, lon, results, demo=args.demo)

    # 標準出力にMarkdownサマリー（チャット報告用）
    print("\n| ハザード種別 | 地点判定 | 周辺約50m最大 | 評価 |")
    print("|---|---|---|---|")
    for r in results:
        print(f"| {r.hazard} | {r.spot} | {r.around} | {r.spot_eval} |")
    worst = max((r.spot_eval for r in results), key=lambda e: {"○": 0, "△": 1, "×": 2}[e])
    print(f"\n総合判定: {worst}（○=非該当 / △=要検討 / ×=基準不適合）")
    print(f"Excel出力: {out}")
    print(f"目視確認URL: {PORTAL_URL.format(lat=lat, lon=lon)}")

    if args.json:
        print(json.dumps(
            {"address": address, "lat": lat, "lon": lon, "overall": worst,
             "results": [vars(r) for r in results]},
            ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
