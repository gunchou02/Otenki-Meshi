"""
recommender.py
お天気メシ レコメンドエンジン (スコアリング方式)

設計方針:
  - 「ポリシー(料理データ)」と「メカニズム(採点ロジック)」を分離する。
    料理を増やす・調整するときは CANDIDATES を編集するだけでよい。
  - if/elif の単一勝者ではなく、複数の信号を「合算」して採点する。
    → 雨の金曜夜なら「雨」と「花金」の両方が効く。
  - 気温などは閾値で急変させず、連続値で滑らかに効かせる (29℃と31℃で激変しない)。
  - 上位候補から softmax で重み付きランダム選択 → 毎回少し変わるが的外れにはならない。
  - なぜその提案になったかを reason として返す (説明可能性 = "AI"の主張を本物にする)。
"""

import json
import math
import os
import random


HOT_WEATHER = ("Clear",)
WET_WEATHER = ("Rain", "Drizzle", "Thunderstorm", "Snow")
CLOUDY_WEATHER = ("Clouds", "Mist", "Fog", "Haze")


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


CATALOG_PATH = os.path.join(os.path.dirname(__file__), "foods.json")


def _load_candidates(path=CATALOG_PATH):
    with open(path, encoding="utf-8") as f:
        candidates = json.load(f)

    required = {"keyword", "msg", "category", "aff"}
    seen = set()
    for c in candidates:
        missing = required - set(c)
        if missing:
            raise ValueError(f"Food candidate is missing keys: {missing}")
        if c["keyword"] in seen:
            raise ValueError(f"Duplicate food keyword: {c['keyword']}")
        if not isinstance(c["aff"], dict) or not c["aff"]:
            raise ValueError(f"Invalid affinity map: {c['keyword']}")
        seen.add(c["keyword"])
    return candidates


CANDIDATES = _load_candidates()
CANDIDATE_BY_KEYWORD = {c["keyword"]: c for c in CANDIDATES}


# 信号 -> 日本語ラベル (reason生成用)
FEATURE_LABEL = {
    "temp_cold": "冷え込み",
    "temp_hot": "暑さ",
    "temp_mild": "過ごしやすい陽気",
    "rainy": "雨",
    "humid": "高い湿度",
    "sunny": "晴れ",
    "cloudy": "曇り空",
    "t_morning": "朝の時間帯",
    "t_lunch": "ランチタイム",
    "t_tea": "おやつの時間",
    "t_dinner": "ディナータイム",
    "t_late": "夜更け",
    "d_friday_night": "金曜の夜",
    "d_monday_lunch": "月曜のお昼",
    "weekend": "週末気分",
}


def build_context_signals(temp, humidity, weather, hour, weekday):
    """
    現在の状況を「信号ベクトル」に変換する。
    気温や湿度は連続値で効かせ、閾値での急変を避ける。
    """
    temp = float(temp)
    humidity = float(humidity)
    sig = {}

    # --- 気温 (連続) ---
    sig["temp_cold"] = _clamp((12 - temp) / 12)        # 12℃以下で立ち上がり、0℃で最大
    sig["temp_hot"] = _clamp((temp - 26) / 8)          # 26℃超で立ち上がり、34℃で最大
    sig["temp_mild"] = _clamp(1 - abs(temp - 20) / 10) # 20℃付近で最大

    # --- 天気 ---
    if weather in WET_WEATHER:
        sig["rainy"] = 1.0
    if weather == "Snow":
        sig["temp_cold"] = max(sig.get("temp_cold", 0), 0.8)
    if weather in HOT_WEATHER:
        sig["sunny"] = 1.0
    if weather in CLOUDY_WEATHER:
        sig["cloudy"] = 1.0

    # --- 湿度 ---
    sig["humid"] = _clamp((humidity - 70) / 30)        # 70%超で立ち上がる

    # --- 時間帯 (境界をあえて重ねて、単調さを減らす) ---
    if 5 <= hour <= 10:
        sig["t_morning"] = 1.0
    if 11 <= hour <= 14:
        sig["t_lunch"] = 1.0
    if 14 <= hour <= 16:
        sig["t_tea"] = 1.0
    if hour >= 17 or hour <= 4:
        sig["t_dinner"] = 1.0
    if hour >= 22 or hour <= 3:
        sig["t_late"] = 1.0

    # --- 曜日 × 時間 (心理的要因) ---
    if weekday == 4 and hour >= 18:
        sig["d_friday_night"] = 1.0
    if weekday == 0 and 11 <= hour <= 14:
        sig["d_monday_lunch"] = 1.0
    if weekday in (5, 6):
        sig["weekend"] = 1.0

    # 0の信号は捨てて軽くする
    return {k: v for k, v in sig.items() if v > 0}


def _score(candidate, sig):
    """候補1件のスコアと、各信号の寄与度を返す。"""
    base = 0.1  # 全候補に薄いベース点 (たまに意外な提案も拾えるように)
    total = base
    contrib = {}
    for feat, aff in candidate["aff"].items():
        if feat in sig:
            c = sig[feat] * aff
            total += c
            contrib[feat] = c
    return total, contrib


def _build_reason(contrib):
    """寄与の大きい信号 上位1〜2件から、提案理由の文を作る。"""
    if not contrib:
        return "今の時間帯に合いそうなお店を選びました。"
    top = sorted(contrib.items(), key=lambda x: x[1], reverse=True)[:2]
    labels = [FEATURE_LABEL.get(f, f) for f, _ in top]
    return " × ".join(labels) + " から選びました。"


def get_candidate(keyword):
    """キーワードに対応する料理候補を返す。フォールバック理由の整合性にも使う。"""
    return CANDIDATE_BY_KEYWORD.get(keyword)


def _recent_categories(recent):
    categories = set()
    for keyword in recent:
        candidate = get_candidate(keyword)
        if candidate:
            categories.add(candidate["category"])
    return categories


def _diverse_keywords(scored, limit=5, excluded_categories=None):
    """検索候補はカテゴリを散らして、似た検索語の連打を避ける。"""
    excluded_categories = set(excluded_categories or [])
    keywords = []
    used_categories = set()

    for candidate, _score_value, _contrib in scored:
        category = candidate["category"]
        if category in excluded_categories:
            continue
        if category in used_categories:
            continue
        keywords.append(candidate["keyword"])
        used_categories.add(category)
        if len(keywords) >= limit:
            return keywords

    for candidate, _score_value, _contrib in scored:
        category = candidate["category"]
        if category in used_categories:
            continue
        if candidate["keyword"] not in keywords:
            keywords.append(candidate["keyword"])
            used_categories.add(category)
        if len(keywords) >= limit:
            break
    return keywords


def _decide_range(sig):
    """状況に応じた検索半径。暑い・雨の日は移動距離を縮める。"""
    if sig.get("temp_hot", 0) > 0.6:
        return 1  # 300m
    if sig.get("rainy", 0) > 0 or sig.get("temp_cold", 0) > 0.7:
        return 2  # 500m
    return 3      # 1000m


def _softmax_pick(items, scores, tau=0.35):
    """
    上位候補からの重み付きランダム選択。
    tau(温度)が小さいほど高得点に集中、大きいほばらつく。
    """
    mx = max(scores)
    weights = [math.exp((s - mx) / tau) for s in scores]
    r = random.random() * sum(weights)
    acc = 0.0
    for item, w in zip(items, weights):
        acc += w
        if r <= acc:
            return item
    return items[-1]


def recommend(temp, humidity, weather, hour, weekday, recent=None, top_k=8):
    """
    メインのレコメンド関数。
    Returns dict:
        keyword, msg, reason, search_range,
        ranked_keywords (フォールバック用の優先順リスト),
        debug (採点の中身: ログ/チューニング用)
    """
    recent = set(recent or [])
    recent_categories = _recent_categories(recent)
    sig = build_context_signals(temp, humidity, weather, hour, weekday)

    scored = []
    for c in CANDIDATES:
        s, contrib = _score(c, sig)
        # 直近で出したキーワードは大きく減点 (同じ提案の連続を防ぐ)
        if c["keyword"] in recent:
            s *= 0.25
        elif c["category"] in recent_categories:
            s *= 0.2
        scored.append((c, s, contrib))

    scored.sort(key=lambda x: (x[1], x[0]["keyword"]), reverse=True)
    fresh_scored = [
        item
        for item in scored
        if item[0]["keyword"] not in recent
        and item[0]["category"] not in recent_categories
    ]
    top = (fresh_scored if len(fresh_scored) >= max(3, top_k // 2) else scored)[:top_k]

    winner = _softmax_pick([t[0] for t in top], [t[1] for t in top])
    winner_contrib = next(contrib for (c, s, contrib) in top if c is winner)

    return {
        "keyword": winner["keyword"],
        "msg": winner["msg"],
        "reason": _build_reason(winner_contrib),
        "category": winner["category"],
        "search_range": _decide_range(sig),
        "ranked_keywords": [c["keyword"] for (c, s, contrib) in scored],
        "ranked_candidates": [
            {
                "keyword": c["keyword"],
                "category": c["category"],
                "msg": c["msg"],
                "reason": _build_reason(contrib),
                "score": round(s, 3),
            }
            for (c, s, contrib) in scored
        ],
        "search_keywords": _diverse_keywords(scored, limit=5, excluded_categories=recent_categories),
        "debug": {
            "signals": sig,
            "recent_categories": sorted(recent_categories),
            "top": [(c["keyword"], c["category"], round(s, 3)) for (c, s, contrib) in top],
        },
    }
