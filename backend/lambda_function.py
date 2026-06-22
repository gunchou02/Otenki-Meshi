import json
import urllib.request
import urllib.parse
import os
import random
import uuid
import boto3
import traceback
from datetime import datetime, timezone, timedelta

import recommender  # ★ スコアリングエンジンを分離

# ==========================================
# 環境変数
# ==========================================
WEATHER_API_KEY = os.environ.get('WEATHER_API_KEY')
HOTPEPPER_API_KEY = os.environ.get('HOTPEPPER_API_KEY')
TABLE_NAME = os.environ.get('LOG_TABLE_NAME', 'OtenkiMeshi_Log_TF')
ALLOWED_ORIGIN = os.environ.get('ALLOWED_ORIGIN', '*')
HTTP_TIMEOUT = 5
DEFAULT_LAT = "35.690921"
DEFAULT_LON = "139.700258"
MAX_RECENT_KEYWORDS = 8
MAX_SEARCH_KEYWORDS = 3

try:
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(TABLE_NAME)
except Exception as e:
    print(f"DynamoDB Init Error: {e}")
    table = None


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_coord(lat, lon):
    lat_num = _to_float(lat)
    lon_num = _to_float(lon)
    if lat_num is None or lon_num is None:
        return DEFAULT_LAT, DEFAULT_LON
    if not (-90 <= lat_num <= 90 and -180 <= lon_num <= 180):
        return DEFAULT_LAT, DEFAULT_LON
    return f"{lat_num:.6f}", f"{lon_num:.6f}"


def _parse_recent(value):
    allowed = {c["keyword"] for c in recommender.CANDIDATES}
    recent = []
    for keyword in (value or "").split(","):
        keyword = keyword.strip()
        if keyword in allowed and keyword not in recent:
            recent.append(keyword)
        if len(recent) >= MAX_RECENT_KEYWORDS:
            break
    return recent


def get_weather_data(lat, lon):
    try:
        if not WEATHER_API_KEY:
            raise RuntimeError("WEATHER_API_KEY is not configured")
        params = urllib.parse.urlencode({
            'lat': lat, 'lon': lon, 'units': 'metric', 'appid': WEATHER_API_KEY,
        })
        url = f"https://api.openweathermap.org/data/2.5/weather?{params}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as res:
            data = json.loads(res.read().decode())
            weather_id = data['weather'][0]['id']
            main_status = data['weather'][0]['main']
            temp = data['main']['temp']
            humidity = data['main']['humidity']
            if weather_id == 800 or weather_id == 801:
                return "Clear", temp, humidity
            return main_status, temp, humidity
    except Exception as e:
        print(f"Weather API Error: {e}")
        traceback.print_exc()
        return "Clear", 20.0, 50


def _shop_key(shop):
    return shop.get('id') or f"{shop.get('name', '')}:{shop.get('address', '')}"


def _merge_unique_shops(current, shops, limit=5):
    seen = {_shop_key(shop) for shop in current}
    for shop in shops:
        key = _shop_key(shop)
        if key in seen:
            continue
        current.append(shop)
        seen.add(key)
        if len(current) >= limit:
            break
    return current


def get_restaurants(lat, lon, keyword, search_range=3, count=20):
    try:
        if not HOTPEPPER_API_KEY:
            raise RuntimeError("HOTPEPPER_API_KEY is not configured")
        base_url = "https://webservice.recruit.co.jp/hotpepper/gourmet/v1/"
        query_params = {
            'key': HOTPEPPER_API_KEY, 'lat': lat, 'lng': lon, 'keyword': keyword,
            'range': search_range, 'order': 4, 'count': count, 'format': 'json',
        }
        full_url = f"{base_url}?{urllib.parse.urlencode(query_params)}"
        req = urllib.request.Request(full_url)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as res:
            data = json.loads(res.read().decode())
            if 'results' in data and 'shop' in data['results']:
                return data['results']['shop']
            return []
    except Exception as e:
        print(f"HotPepper API Error: {e}")
        traceback.print_exc()
        return []


def get_restaurants_for_keywords(lat, lon, keywords, search_range=3, limit=5):
    shops = []
    for keyword in keywords[:MAX_SEARCH_KEYWORDS]:
        found = get_restaurants(lat, lon, keyword, search_range)
        if found:
            random.shuffle(found)
            _merge_unique_shops(shops, found, limit=limit)
        if len(shops) >= limit:
            break
    return shops


def save_log_to_dynamodb(lat, lon, weather, temp, keyword, logic):
    if table is None:
        return
    try:
        JST = timezone(timedelta(hours=9))
        table.put_item(Item={
            'request_id': str(uuid.uuid4()),
            'timestamp': datetime.now(JST).isoformat(),
            'location': f"{lat},{lon}",
            'weather': weather,
            'temp': str(temp),
            'recommended_keyword': keyword,
            'logic_used': logic
        })
    except Exception as e:
        print(f"DynamoDB Write Error: {e}")


def lambda_handler(event, context):
    headers = {
        'Access-Control-Allow-Origin': ALLOWED_ORIGIN,
        'Access-Control-Allow-Headers': 'Content-Type',
        'Access-Control-Allow-Methods': 'OPTIONS,GET'
    }

    try:
        params = event.get('queryStringParameters') or {}
        lat = params.get('lat')
        lon = params.get('lon')
        lat, lon = _normalize_coord(lat, lon)

        # 直近で出した提案 (フロントから "recent=ラーメン,焼肉" のように渡す)。同じ提案の連続を防ぐ。
        recent = _parse_recent(params.get('recent'))

        weather, temp, humidity = get_weather_data(lat, lon)

        JST = timezone(timedelta(hours=9))
        now = datetime.now(JST)

        # ★ レコメンドはエンジンに委譲。複数信号を合算したスコアで決まる。
        rec = recommender.recommend(
            temp=temp, humidity=humidity, weather=weather,
            hour=now.hour, weekday=now.weekday(), recent=recent,
        )

        keyword = rec["keyword"]
        msg = rec["msg"]
        reason = rec["reason"]
        search_range = rec["search_range"]
        logic_reason = f"score | range={search_range} | top={rec['debug']['top'][:3]}"

        # --- 検索 + フォールバック ---
        search_keywords = rec.get("search_keywords") or [keyword]
        shops = get_restaurants_for_keywords(lat, lon, search_keywords, search_range)

        # [再試行1] 0件なら、まず半径を広げる
        if not shops and search_range < 3:
            shops = get_restaurants_for_keywords(lat, lon, search_keywords, 3)
            logic_reason += " (range extended)"

        # [再試行2] それでも0件なら、汎用ワードに逃げる前に
        #           「次に点数の高い候補」を順に試す (テーマを保ったままフォールバック)
        if not shops:
            for alt in rec["ranked_candidates"][1:6]:
                shops = get_restaurants_for_keywords(lat, lon, [alt["keyword"]], 5)
                if shops:
                    keyword = alt["keyword"]
                    msg = alt["msg"]
                    reason = f"検索結果に合わせて「{keyword}」に切り替えました。"
                    logic_reason += f" (re-rank fallback: {keyword})"
                    break

        # [最終] まだ0件なら時間帯ベースの汎用ワードで広域検索
        if not shops:
            generic = "ランチ" if 11 <= now.hour < 15 else "カフェ" if now.hour < 17 else "居酒屋"
            shops = get_restaurants_for_keywords(lat, lon, [generic], 5)
            keyword = generic
            msg = "近くにお店が見つからなかったので、周辺の人気スポットを探してきました！🏃"
            reason = f"周辺店舗の見つかりやすさを優先して「{generic}」で探しました。"
            logic_reason += f" (final fallback: {generic})"

        save_log_to_dynamodb(lat, lon, weather, temp, keyword, logic_reason)

        return {
            'statusCode': 200,
            'headers': headers,
            'body': json.dumps({
                'weather': weather,
                'temp': temp,
                'humidity': humidity,
                'message': msg,
                'reason': reason,        # ★ "なぜこれ？" をフロントに渡す
                'keyword': keyword,
                'shops': shops,
            }, ensure_ascii=False)
        }

    except Exception as e:
        print("************ CRITICAL ERROR ************")
        print(f"Error: {str(e)}")
        traceback.print_exc()
        return {
            'statusCode': 500,
            'headers': headers,
            'body': json.dumps({'error': 'Internal Server Error'}, ensure_ascii=False)
        }
