
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
地震・津波・臨時情報 監視 → Gmail 通知システム
 
送信先: 201-25044@g.hiroshima-c.ed.jp
 
メール種別(それぞれ別メール)
  1. 「地震速報」                 : 震動検出(震央地名/M/深さ/最大震度/各地の震度)
  2. 「緊急地震速報　大地震です」 : 緊急地震速報(警報)発表時(データは1と同じ項目)
  3. 「津波情報」                 : 津波注意報/警報/大津波警報/若干の海面変動
  4. 「南海トラフ臨時情報」/「北海道・三陸沖後発地震注意情報」
  5. 「海外で地震が発生」         : 日本を除く M6以上
 
使い方
  export GMAIL_USER="あなたのGmailアドレス"
  export GMAIL_APP_PASSWORD="Googleアプリパスワード(16桁)"
  python3 jishin_notifier.py          # 常時監視
  python3 jishin_notifier.py --test   # 全種類のテストメール送信
"""
import json
import os
import re
import smtplib
import ssl
import sys
import time
import traceback
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
 
TO_ADDR = "201-25044@g.hiroshima-c.ed.jp"
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")
 
JST = timezone(timedelta(hours=9))
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sent_state.json")
 
# ---- データ取得元 ----
# 強震モニタ/Yahoo!防災速報と同じ気象庁・防災科研の情報を配信している公開API
P2P_URL = ("https://api.p2pquake.net/v2/history"
           "?codes=551&codes=552&codes=556&limit=30")
JMA_EQVOL_URL = "https://www.data.jma.go.jp/developer/xml/feed/eqvol.xml"
USGS_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
 
POLL_P2P = 5      # 秒
POLL_JMA = 30
POLL_USGS = 120
 
SCALE = {-1: "不明", 0: "0", 10: "1", 20: "2", 30: "3", 40: "4",
         45: "5弱", 50: "5強", 55: "6弱", 60: "6強", 70: "7"}
TSUNAMI_GRADE = {"MajorWarning": "大津波警報", "Warning": "津波警報",
                 "Watch": "津波注意報", "Unknown": "不明"}
ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"]
 
 
# ---------------- 共通 ----------------
def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "jishin-notifier/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()
 
 
def log(msg):
    print(f"[{datetime.now(JST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)
 
 
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"seen": {}, "initialized": {}}
 
 
def save_state(st):
    # seen が肥大化しないよう各カテゴリ最新500件のみ保持
    for k, v in st["seen"].items():
        if len(v) > 500:
            st["seen"][k] = v[-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
 
 
def send_mail(subject, data: dict, text_lines):
    """data をJSON化し、読みやすい本文と併せてGmailで送信"""
    if not GMAIL_USER or not GMAIL_PASS:
        raise RuntimeError("環境変数 GMAIL_USER / GMAIL_APP_PASSWORD が未設定です")
    body = "\n".join(text_lines)
    body += "\n\n--- データ(JSON) ---\n" + json.dumps(data, ensure_ascii=False, indent=2)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = TO_ADDR
    msg.set_content(body)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30) as s:
        s.login(GMAIL_USER, GMAIL_PASS)
        s.send_message(msg)
    log(f"送信: {subject}")
 
 
# ---------------- 1,2: 地震速報 / 緊急地震速報 ----------------
def eq_data_from_551(e):
    eq = e.get("earthquake", {})
    hypo = eq.get("hypocenter", {})
    depth = hypo.get("depth", -1)
    mag = hypo.get("magnitude", -1)
    points = [{"都道府県": p.get("pref", ""), "地点": p.get("addr", ""),
               "震度": SCALE.get(p.get("scale", -1), "不明")}
              for p in e.get("points", [])]
    # 震度の大きい順
    order = {v: k for k, v in SCALE.items()}
    points.sort(key=lambda p: order.get(p["震度"], -1), reverse=True)
    return {
        "発生時刻": eq.get("time", ""),
        "震央地名": hypo.get("name") or "調査中",
        "マグニチュード": "不明" if mag in (-1, None) else mag,
        "震源深さ": ("不明" if depth in (-1, None)
                    else ("ごく浅い" if depth == 0 else f"約{depth}km")),
        "最大震度": SCALE.get(eq.get("maxScale", -1), "不明"),
        "各地の震度情報": points,
    }
 
 
def eq_data_from_556(e):
    """緊急地震速報(警報)。地震速報と同じ項目で作成(震度は予測値)"""
    eq = e.get("earthquake", {})
    hypo = eq.get("hypocenter", {})
    depth = hypo.get("depth", -1)
    mag = hypo.get("magnitude", -1)
    areas = []
    max_scale = -1
    for a in e.get("areas", []):
        to = a.get("scaleTo", a.get("scaleFrom", -1))
        frm = a.get("scaleFrom", -1)
        max_scale = max(max_scale, frm if to in (None, 99) else to)
        s_from = SCALE.get(frm, "不明")
        s_to = "以上" if to == 99 else SCALE.get(to, "不明")
        areas.append({"都道府県": a.get("pref", ""), "地域": a.get("name", ""),
                      "震度(予測)": s_from + ("程度以上" if to == 99 else
                                              ("" if to == frm else f"〜{s_to}"))})
    return {
        "発生時刻": eq.get("originTime", ""),
        "震央地名": hypo.get("name") or hypo.get("reduceName") or "調査中",
        "マグニチュード": "不明" if mag in (-1, None) else mag,
        "震源深さ": ("不明" if depth in (-1, None)
                    else ("ごく浅い" if depth == 0 else f"約{depth}km")),
        "最大震度": SCALE.get(max_scale, "不明") + "(予測)",
        "各地の震度情報": areas,
    }
 
 
def eq_text(d, head):
    lines = [head, "",
             f"発生時刻　: {d['発生時刻']}",
             f"震央地名　: {d['震央地名']}",
             f"マグニチュード: {d['マグニチュード']}",
             f"震源の深さ: {d['震源深さ']}",
             f"最大震度　: {d['最大震度']}",
             "", "【各地の震度情報】"]
    for p in d["各地の震度情報"]:
        if "震度" in p:
            lines.append(f"  震度{p['震度']}  {p['都道府県']} {p['地点']}")
        else:
            lines.append(f"  {p['震度(予測)']}  {p['都道府県']} {p['地域']}")
    if not d["各地の震度情報"]:
        lines.append("  (情報なし)")
    return lines
 
 
def send_quake(e):
    d = eq_data_from_551(e)
    send_mail("地震速報", d, eq_text(d, "震動を検出しました。"))
 
 
def send_eew(e):
    d = eq_data_from_556(e)
    send_mail("緊急地震速報　大地震です", d,
              eq_text(d, "緊急地震速報(警報)が発表されました。強い揺れに警戒してください。"))
 
 
# ---------------- 3: 津波情報 ----------------
def minutes_text(first):
    if not first:
        return "不明"
    cond = first.get("condition")
    arr = first.get("arrivalTime")
    if cond:
        return cond  # 例: ただちに津波来襲と予想
    if not arr:
        return "不明"
    now = datetime.now(JST)
    t = None
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(arr, fmt)
            break
        except ValueError:
            continue
    if t is None:
        return str(arr)
    if fmt == "%H:%M":
        t = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if t < now - timedelta(hours=12):
            t += timedelta(days=1)
    elif t.tzinfo is None:
        t = t.replace(tzinfo=JST)
    mins = int((t - now).total_seconds() // 60)
    if mins <= 0:
        return f"{arr}(すでに到達している可能性あり)"
    return f"{arr}(あと約{mins}分)"
 
 
def send_tsunami_552(e):
    areas = []
    for a in e.get("areas", []):
        mh = a.get("maxHeight") or {}
        areas.append({
            "種別": TSUNAMI_GRADE.get(a.get("grade"), "不明"),
            "発表された地域": a.get("name", ""),
            "予想最大高さ": mh.get("description") or
                         (f"{mh['value']}m" if mh.get("value") is not None else "不明"),
            "到達までの時間": minutes_text(a.get("firstHeight")),
        })
    if not areas:
        return
    d = {"発表時刻": e.get("issue", {}).get("time", ""), "津波情報": areas}
    lines = ["津波に関する情報が発表されました。", ""]
    for a in areas:
        lines.append(f"[{a['種別']}] {a['発表された地域']}  "
                     f"予想最大高さ: {a['予想最大高さ']} / 到達: {a['到達までの時間']}")
    send_mail("津波情報", d, lines)
 
 
def send_tsunami_slight(e):
    """551で domesticTsunami=NonEffective (若干の海面変動が予想される)"""
    eq = e.get("earthquake", {})
    d = {"発表時刻": e.get("time", ""),
         "津波情報": [{"種別": "若干の海面変動",
                     "発表された地域": "地域別の詳細は気象庁発表を確認",
                     "予想最大高さ": "被害の心配なし(若干の海面変動)",
                     "到達までの時間": "不明"}],
         "震央地名": eq.get("hypocenter", {}).get("name", "")}
    send_mail("津波情報", d, [
        "若干の海面変動が予想されます。被害の心配はありません。",
        f"震央地名: {d['震央地名']}"])
 
 
# ---------------- 4: 臨時情報 ----------------
def classify_nankai(text):
    if "巨大地震警戒" in text:
        return "巨大地震警戒"
    if "巨大地震注意" in text:
        return "巨大地震注意"
    if "調査中" in text:
        return "調査中"
    return None
 
 
def check_jma(st, first_run):
    root = ET.fromstring(http_get(JMA_EQVOL_URL))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    seen = st["seen"].setdefault("jma", [])
    for en in root.findall("a:entry", ns):
        eid = en.findtext("a:id", "", ns)
        title = en.findtext("a:title", "", ns)
        if eid in seen:
            continue
        is_nankai = "南海トラフ地震臨時情報" in title
        is_hokusan = "北海道・三陸沖後発地震注意情報" in title
        if not (is_nankai or is_hokusan):
            seen.append(eid)
            continue
        if first_run:
            seen.append(eid)
            continue
        link = en.find("a:link", ns)
        text = title + " " + en.findtext("a:content", "", ns)
        try:
            xml = http_get(link.get("href")).decode("utf-8", "ignore")
            text += " " + re.sub(r"<[^>]+>", " ", xml)
        except Exception:
            pass
        issued = en.findtext("a:updated", "", ns)
        if is_nankai:
            kind = classify_nankai(text)
            if kind is None:  # 調査終了など(依頼の3種類以外)は対象外
                seen.append(eid)
                continue
            send_mail("南海トラフ臨時情報", {"発表時刻": issued, "種類": kind},
                      [f"南海トラフ地震臨時情報が発表されました。", f"種類: {kind}"])
        else:
            kind = "巨大地震注意" if "巨大地震注意" in text else "後発地震注意情報"
            send_mail("北海道・三陸沖後発地震注意情報",
                      {"発表時刻": issued, "種類": kind},
                      ["北海道・三陸沖後発地震注意情報が発表されました。", f"種類: {kind}"])
        seen.append(eid)
 
 
# ---------------- 5: 海外地震 ----------------
def mmi_text(p):
    v = p.get("mmi")
    if v is None:
        v = p.get("cdi")
    if v is None:
        return None
    n = int(round(v))
    return f"{ROMAN[max(1, min(12, n))]} ({v:.1f})"
 
 
# ---- 海外地震の震源地を日本語化 ----
PLACE_JA = {
 "afghanistan": "アフガニスタン",
 "albania": "アルバニア",
 "algeria": "アルジェリア",
 "angola": "アンゴラ",
 "argentina": "アルゼンチン",
 "armenia": "アルメニア",
 "australia": "オーストラリア",
 "azerbaijan": "アゼルバイジャン",
 "bangladesh": "バングラデシュ",
 "bolivia": "ボリビア",
 "bosnia and herzegovina": "ボスニア・ヘルツェゴビナ",
 "brazil": "ブラジル",
 "bulgaria": "ブルガリア",
 "myanmar": "ミャンマー",
 "burma": "ミャンマー",
 "cameroon": "カメルーン",
 "canada": "カナダ",
 "chile": "チリ",
 "china": "中国",
 "colombia": "コロンビア",
 "costa rica": "コスタリカ",
 "croatia": "クロアチア",
 "cuba": "キューバ",
 "cyprus": "キプロス",
 "democratic republic of the congo": "コンゴ民主共和国",
 "dominican republic": "ドミニカ共和国",
 "ecuador": "エクアドル",
 "egypt": "エジプト",
 "el salvador": "エルサルバドル",
 "ethiopia": "エチオピア",
 "fiji": "フィジー",
 "france": "フランス",
 "georgia": "ジョージア",
 "germany": "ドイツ",
 "greece": "ギリシャ",
 "guatemala": "グアテマラ",
 "haiti": "ハイチ",
 "honduras": "ホンジュラス",
 "iceland": "アイスランド",
 "india": "インド",
 "indonesia": "インドネシア",
 "iran": "イラン",
 "iraq": "イラク",
 "israel": "イスラエル",
 "italy": "イタリア",
 "jamaica": "ジャマイカ",
 "jordan": "ヨルダン",
 "kazakhstan": "カザフスタン",
 "kenya": "ケニア",
 "kyrgyzstan": "キルギス",
 "laos": "ラオス",
 "lebanon": "レバノン",
 "libya": "リビア",
 "madagascar": "マダガスカル",
 "malawi": "マラウイ",
 "malaysia": "マレーシア",
 "mexico": "メキシコ",
 "mongolia": "モンゴル",
 "morocco": "モロッコ",
 "mozambique": "モザンビーク",
 "nepal": "ネパール",
 "new zealand": "ニュージーランド",
 "nicaragua": "ニカラグア",
 "nigeria": "ナイジェリア",
 "north korea": "北朝鮮",
 "south korea": "韓国",
 "pakistan": "パキスタン",
 "panama": "パナマ",
 "papua new guinea": "パプアニューギニア",
 "peru": "ペルー",
 "philippines": "フィリピン",
 "philippine islands": "フィリピン諸島",
 "portugal": "ポルトガル",
 "puerto rico": "プエルトリコ",
 "romania": "ルーマニア",
 "russia": "ロシア",
 "samoa": "サモア",
 "american samoa": "米領サモア",
 "saudi arabia": "サウジアラビア",
 "serbia": "セルビア",
 "solomon islands": "ソロモン諸島",
 "somalia": "ソマリア",
 "south africa": "南アフリカ",
 "spain": "スペイン",
 "sri lanka": "スリランカ",
 "sudan": "スーダン",
 "syria": "シリア",
 "taiwan": "台湾",
 "tajikistan": "タジキスタン",
 "tanzania": "タンザニア",
 "thailand": "タイ",
 "tonga": "トンガ",
 "trinidad and tobago": "トリニダード・トバゴ",
 "tunisia": "チュニジア",
 "turkey": "トルコ",
 "turkmenistan": "トルクメニスタン",
 "uganda": "ウガンダ",
 "ukraine": "ウクライナ",
 "united kingdom": "イギリス",
 "vanuatu": "バヌアツ",
 "venezuela": "ベネズエラ",
 "vietnam": "ベトナム",
 "yemen": "イエメン",
 "zambia": "ザンビア",
 "zimbabwe": "ジンバブエ",
 "uzbekistan": "ウズベキスタン",
 "new caledonia": "ニューカレドニア",
 "guam": "グアム",
 "northern mariana islands": "北マリアナ諸島",
 "mariana islands": "マリアナ諸島",
 "vanuatu islands": "バヌアツ諸島",
 "kuril islands": "千島列島",
 "kamchatka": "カムチャツカ",
 "komandorskiye": "コマンドルスキー諸島",
 "sakhalin": "サハリン",
 "okhotsk": "オホーツク",
 "kermadec islands": "ケルマデック諸島",
 "loyalty islands": "ロイヤルティ諸島",
 "south sandwich islands": "サウスサンドウィッチ諸島",
 "south georgia island": "サウスジョージア島",
 "macquarie island": "マッコーリー島",
 "easter island": "イースター島",
 "galapagos": "ガラパゴス諸島",
 "azores": "アゾレス諸島",
 "andaman islands": "アンダマン諸島",
 "nicobar islands": "ニコバル諸島",
 "new britain": "ニューブリテン島",
 "new ireland": "ニューアイルランド島",
 "bougainville": "ブーゲンビル",
 "santa cruz islands": "サンタクルーズ諸島",
 "aleutian islands": "アリューシャン列島",
 "banda sea": "バンダ海",
 "molucca sea": "モルッカ海",
 "celebes sea": "セレベス海",
 "drake passage": "ドレーク海峡",
 "scotia sea": "スコシア海",
 "mid-atlantic ridge": "大西洋中央海嶺",
 "southern east pacific rise": "南東太平洋海膨",
 "east pacific rise": "東太平洋海膨",
 "pacific-antarctic ridge": "太平洋・南極海嶺",
 "southwest indian ridge": "南西インド洋海嶺",
 "central mid-atlantic ridge": "中央大西洋海嶺",
 "indian ocean": "インド洋",
 "south pacific ocean": "南太平洋",
 "north pacific ocean": "北太平洋",
 "central america": "中央アメリカ",
 "antarctica": "南極",
 "east of the south sandwich islands": "サウスサンドウィッチ諸島の東",
 "alaska": "アラスカ州",
 "california": "カリフォルニア州",
 "oregon": "オレゴン州",
 "washington": "ワシントン州",
 "nevada": "ネバダ州",
 "idaho": "アイダホ州",
 "montana": "モンタナ州",
 "utah": "ユタ州",
 "wyoming": "ワイオミング州",
 "oklahoma": "オクラホマ州",
 "texas": "テキサス州",
 "hawaii": "ハワイ州",
 "new mexico": "ニューメキシコ州",
 "arizona": "アリゾナ州",
 "colorado": "コロラド州",
 "kansas": "カンザス州",
 "missouri": "ミズーリ州",
 "tennessee": "テネシー州",
 "arkansas": "アーカンソー州",
 "indiana": "インディアナ州",
 "british columbia": "ブリティッシュコロンビア州",
 "yukon": "ユーコン準州",
 "united states": "アメリカ合衆国"
}
DIR_JA = {"N": "北", "NNE": "北北東", "NE": "北東", "ENE": "東北東", "E": "東", "ESE": "東南東", "SE": "南東", "SSE": "南南東", "S": "南", "SSW": "南南西", "SW": "南西", "WSW": "西南西", "W": "西", "WNW": "西北西", "NW": "北西", "NNW": "北北西"}
PLACE_KEYS = sorted(PLACE_JA, key=len, reverse=True)
 
 
def place_ja(place):
    """USGSの英語表記(例: 10 km SW of Pisco, Peru)を日本語にする。訳せなければ None"""
    s = (place or "").strip()
    m = re.match(r"^(\d+(?:\.\d+)?) km ([NSEW]{1,3}) of (.+)$", s)
    rest = m.group(3) if m else s
    parts = [x.strip() for x in rest.split(",")]
    ja = PLACE_JA.get(parts[-1].lower())
    if ja is None:
        low = rest.lower()
        for k in PLACE_KEYS:
            if re.search(r"\b" + re.escape(k) + r"\b", low):
                ja = PLACE_JA[k]
                break
    if ja is None:
        return None
    city = ", ".join(PLACE_JA.get(x.lower(), x) for x in parts[:-1]) if len(parts) > 1 else ""
    head = f"{ja}・{city}" if city else ja
    if m:
        return f"{head}の{DIR_JA.get(m.group(2), '')}約{m.group(1)}km付近"
    return f"{head}付近"
 
 
def place_display(place):
    ja = place_ja(place)
    return f"{ja}(原文: {place})" if ja else place
 
 
def check_usgs(st, first_run):
    data = json.loads(http_get(USGS_URL))
    seen = st["seen"].setdefault("usgs", [])
    now_ms = time.time() * 1000
    for f in data.get("features", []):
        p = f["properties"]
        fid = f["id"]
        if fid in seen:
            continue
        mag = p.get("mag")
        place = p.get("place") or ""
        if mag is None or mag < 6.0 or "Japan" in place:
            seen.append(fid)
            continue
        if first_run:
            seen.append(fid)
            continue
        mm = mmi_text(p)
        age_min = (now_ms - p.get("time", now_ms)) / 60000
        if mm is None and age_min < 30:
            continue  # 改正メルカリ震度階級が出るまで最大30分待つ(次回再判定)
        d = {"発生時刻(JST)": datetime.fromtimestamp(p["time"] / 1000, JST).strftime("%Y-%m-%d %H:%M:%S"),
             "マグニチュード": mag, "震源地": place_display(place),
             "改正メルカリ震度階級": mm or "未算出"}
        send_mail("海外で地震が発生", d, [
            "海外でM6以上の地震が発生しました。", "",
            f"発生時刻: {d['発生時刻(JST)']}",
            f"マグニチュード: M{mag}",
            f"震源地: {d['震源地']}",
            f"改正メルカリ震度階級: {d['改正メルカリ震度階級']}"])
        seen.append(fid)
 
 
# ---------------- P2P(地震/EEW/津波) ----------------
def check_p2p(st, first_run):
    items = json.loads(http_get(P2P_URL))
    seen = st["seen"].setdefault("p2p", [])
    for e in reversed(items):  # 古い順
        eid = e.get("id")
        if eid in seen:
            continue
        code = e.get("code")
        if first_run:
            seen.append(eid)
            continue
        try:
            if code == 551:
                if e.get("issue", {}).get("type") == "Foreign":
                    pass
                elif e.get("earthquake", {}).get("maxScale", -1) != -1 or e.get("points"):
                    send_quake(e)
                if e.get("earthquake", {}).get("domesticTsunami") == "NonEffective":
                    send_tsunami_slight(e)
            elif code == 556:
                if not e.get("test") and not e.get("cancelled"):
                    # 同一地震の第2報以降は重複送信しない
                    ev = e.get("issue", {}).get("eventId", eid)
                    key = f"eew:{ev}"
                    if key not in seen:
                        send_eew(e)
                        seen.append(key)
            elif code == 552:
                if not e.get("cancelled"):
                    send_tsunami_552(e)
            seen.append(eid)
        except Exception:
            log("送信失敗(次回再試行): " + traceback.format_exc())
 
 
# ---------------- テスト ----------------
def run_test():
    log("テストメールを送信します")
    q = {"time": "テスト", "earthquake": {"time": "2026/01/01 00:00:00",
         "hypocenter": {"name": "(テスト)", "depth": 10, "magnitude": 6.0}, "maxScale": 50},
         "points": [{"pref": "広島県", "addr": "広島市", "scale": 50}]}
    send_quake(q)
    send_eew({"earthquake": {"originTime": "テスト", "hypocenter": {"name": "(テスト)", "depth": 10, "magnitude": 7.0}},
              "areas": [{"pref": "広島県", "name": "広島県南部", "scaleFrom": 50, "scaleTo": 99}]})
    send_tsunami_552({"issue": {"time": "テスト"}, "areas": [{
        "grade": "Warning", "name": "(テスト)沿岸", "immediate": True,
        "maxHeight": {"description": "3m"}, "firstHeight": {"condition": "ただちに津波来襲と予想"}}]})
    send_mail("南海トラフ臨時情報", {"発表時刻": "テスト", "種類": "調査中"},
              ["(テスト) 南海トラフ地震臨時情報", "種類: 調査中"])
    send_mail("北海道・三陸沖後発地震注意情報", {"発表時刻": "テスト", "種類": "巨大地震注意"},
              ["(テスト) 北海道・三陸沖後発地震注意情報", "種類: 巨大地震注意"])
    send_mail("海外で地震が発生", {"マグニチュード": 6.5, "震源地": "(テスト)", "改正メルカリ震度階級": "VII (7.0)"},
              ["(テスト) 海外で地震が発生", "M6.5 / (テスト) / 改正メルカリ震度階級 VII"])
 
 
# ---------------- メイン ----------------
def main():
    if "--test" in sys.argv:
        run_test()
        return
    if not GMAIL_USER or not GMAIL_PASS:
        sys.exit("GMAIL_USER と GMAIL_APP_PASSWORD を環境変数に設定してください")
    st = load_state()
    init = st.setdefault("initialized", {})
    jobs = [("p2p", check_p2p, POLL_P2P), ("jma", check_jma, POLL_JMA), ("usgs", check_usgs, POLL_USGS)]
    last = {k: 0 for k, _, _ in jobs}
    log("監視を開始しました(起動前から出ていた情報も通知します)")
    while True:
        now = time.time()
        for name, fn, interval in jobs:
            if now - last[name] < interval:
                continue
            last[name] = now
            try:
                # 起動前から出ていた情報も通知する(初回スキップなし)。
                # 送信済みIDは sent_state.json に記録され、再起動しても二重送信しない。
                fn(st, False)
                init[name] = True
                save_state(st)
            except Exception as ex:
                log(f"{name} エラー: {ex}")
        time.sleep(1)
 
 
if __name__ == "__main__":
    main()
