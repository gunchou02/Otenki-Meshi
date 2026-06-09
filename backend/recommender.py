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

import math
import random


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


# ==========================================
# 料理候補カタログ
# aff = この料理が「どの状況に向くか」の親和度 (0.0〜1.0)。
#       コンテキスト信号との内積で点数が決まる。
# ==========================================
CANDIDATES = [
    # --- 温かい系 (寒い・雨に強い) ---
    {"keyword": "鍋",        "msg": "芯から温まる鍋で、ほっと一息つきませんか🍲",       "aff": {"temp_cold": 0.9, "t_dinner": 0.5, "rainy": 0.3}},
    {"keyword": "ラーメン",   "msg": "熱々のラーメンが体に染みる一杯を🍜",               "aff": {"temp_cold": 0.7, "t_lunch": 0.4, "t_dinner": 0.4, "t_late": 0.6, "rainy": 0.4}},
    {"keyword": "うどん",     "msg": "やさしい出汁のうどんで温まりましょう。",            "aff": {"temp_cold": 0.5, "rainy": 0.4, "t_lunch": 0.4}},
    {"keyword": "おでん",     "msg": "じっくり煮込んだおでんが恋しい季節です。",          "aff": {"temp_cold": 0.7, "t_dinner": 0.4, "t_late": 0.5}},
    {"keyword": "スープカレー", "msg": "スパイスの効いたスープカレーでポカポカに🌶️",      "aff": {"temp_cold": 0.6, "humid": 0.3}},

    # --- さっぱり・冷たい系 (暑い・蒸す日に強い) ---
    {"keyword": "冷麺",       "msg": "つるっと冷麺で涼やかに🥶",                        "aff": {"temp_hot": 0.9, "humid": 0.4, "t_lunch": 0.5}},
    {"keyword": "冷やし中華",  "msg": "ジメジメした日は、さっぱり冷やし中華が最高！",      "aff": {"temp_hot": 0.7, "humid": 0.7, "t_lunch": 0.6}},
    {"keyword": "そうめん",    "msg": "喉ごし爽やかなそうめんでクールダウン。",            "aff": {"temp_hot": 0.6, "humid": 0.5, "t_lunch": 0.4}},
    {"keyword": "かき氷",     "msg": "ひんやりかき氷で涼みましょう🍧",                   "aff": {"temp_hot": 0.8, "t_tea": 0.6}},
    {"keyword": "うなぎ",     "msg": "暑さに負けないよう、うなぎでスタミナ補給！",        "aff": {"temp_hot": 0.5, "t_dinner": 0.4}},

    # --- 湿度・スパイシー系 ---
    {"keyword": "エスニック",  "msg": "蒸し暑い日はスパイシーなエスニックが合います。",    "aff": {"humid": 0.7, "temp_hot": 0.4, "t_dinner": 0.4}},
    {"keyword": "激辛",       "msg": "ガツンと辛いもので気合いを入れましょう🌶️",        "aff": {"humid": 0.5, "d_monday_lunch": 0.8, "t_lunch": 0.4}},

    # --- 雨・駅近系 ---
    {"keyword": "駅近 ランチ", "msg": "雨に濡れにくい駅近のお店を厳選しました☔️",        "aff": {"rainy": 0.8, "t_lunch": 0.6}},
    {"keyword": "デリバリー",  "msg": "雨足が強いですね。デリバリー対応店はいかが？",      "aff": {"rainy": 0.7}},

    # --- 穏やかな陽気・定番ランチ ---
    {"keyword": "定食",       "msg": "バランスの良い定食で午後も元気に🍱",              "aff": {"temp_mild": 0.5, "t_lunch": 0.7}},
    {"keyword": "パスタ",     "msg": "美味しいパスタでランチタイムを🍝",                "aff": {"temp_mild": 0.5, "t_lunch": 0.5, "t_dinner": 0.3}},
    {"keyword": "ハンバーガー", "msg": "ガッツリ気分ならハンバーガー！🍔",               "aff": {"temp_mild": 0.4, "t_lunch": 0.5}},
    {"keyword": "オムライス",  "msg": "ふわふわ卵のオムライスで幸せ気分。",              "aff": {"temp_mild": 0.4, "t_lunch": 0.4}},

    # --- カフェ・ティータイム ---
    {"keyword": "パンケーキ",  "msg": "午後のひとときに甘いパンケーキ🥞",                "aff": {"t_tea": 0.8}},
    {"keyword": "カフェ",     "msg": "コーヒーの香りでリラックスタイム☕️",             "aff": {"t_tea": 0.7, "t_morning": 0.5, "temp_mild": 0.3}},
    {"keyword": "スイーツ",    "msg": "疲れた頭には甘いスイーツが一番🍰",               "aff": {"t_tea": 0.7}},

    # --- ディナー・社交系 ---
    {"keyword": "居酒屋",     "msg": "今日もお疲れ様！近くで乾杯🍻",                    "aff": {"t_dinner": 0.7, "d_friday_night": 1.0}},
    {"keyword": "焼き鳥",     "msg": "香ばしい焼き鳥とビール、最高ですね。",            "aff": {"t_dinner": 0.6, "d_friday_night": 0.7}},
    {"keyword": "焼肉",       "msg": "ガッツリ焼肉でスタミナ補給！🥩",                  "aff": {"t_dinner": 0.7, "temp_cold": 0.3}},
    {"keyword": "バル",       "msg": "おしゃれなバルでワインなんていかが？🍷",          "aff": {"t_dinner": 0.5, "d_friday_night": 0.8}},
    {"keyword": "餃子",       "msg": "肉汁たっぷりの餃子でご飯が進む！🥟",              "aff": {"t_dinner": 0.5, "t_late": 0.5}},

    # --- 月曜の活力 ---
    {"keyword": "カツ丼",     "msg": "今週も「勝つ」！カツ丼でエネルギーチャージ💪",     "aff": {"d_monday_lunch": 0.9, "t_lunch": 0.5}},
    {"keyword": "カレー",     "msg": "スパイスの力で午後も活性化🍛",                    "aff": {"d_monday_lunch": 0.6, "t_lunch": 0.4, "temp_mild": 0.3}},

    # --- モーニング ---
    {"keyword": "カフェ モーニング", "msg": "少し早起きして、優雅なモーニング☕️",       "aff": {"t_morning": 1.0}},
    {"keyword": "パン屋",     "msg": "焼きたてパンの香りで一日をスタート🥐",            "aff": {"t_morning": 0.8}},
    {"keyword": "おにぎり",    "msg": "日本の朝はやっぱりおにぎりとお味噌汁🍙",          "aff": {"t_morning": 0.6}},
]


# 信号 -> 日本語ラベル (reason生成用)
FEATURE_LABEL = {
    "temp_cold": "冷え込み",
    "temp_hot": "暑さ",
    "temp_mild": "過ごしやすい陽気",
    "rainy": "雨",
    "humid": "高い湿度",
    "t_morning": "朝の時間帯",
    "t_lunch": "ランチタイム",
    "t_tea": "おやつの時間",
    "t_dinner": "ディナータイム",
    "t_late": "夜更け",
    "d_friday_night": "金曜の夜",
    "d_monday_lunch": "月曜のお昼",
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
    if weather in ("Rain", "Drizzle", "Thunderstorm", "Snow"):
        sig["rainy"] = 1.0
    if weather == "Snow":
        sig["temp_cold"] = max(sig.get("temp_cold", 0), 0.8)

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
    sig = build_context_signals(temp, humidity, weather, hour, weekday)

    scored = []
    for c in CANDIDATES:
        s, contrib = _score(c, sig)
        # 直近で出したキーワードは大きく減点 (同じ提案の連続を防ぐ)
        if c["keyword"] in recent:
            s *= 0.3
        scored.append((c, s, contrib))

    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[:top_k]

    winner = _softmax_pick([t[0] for t in top], [t[1] for t in top])
    winner_contrib = next(contrib for (c, s, contrib) in top if c is winner)

    return {
        "keyword": winner["keyword"],
        "msg": winner["msg"],
        "reason": _build_reason(winner_contrib),
        "search_range": _decide_range(sig),
        "ranked_keywords": [c["keyword"] for (c, s, contrib) in scored],
        "debug": {
            "signals": sig,
            "top": [(c["keyword"], round(s, 3)) for (c, s, contrib) in top],
        },
    }
