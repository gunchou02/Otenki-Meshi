import json
import urllib.request
import urllib.parse
import os
import random
import uuid
import boto3
import traceback
from datetime import datetime, timezone, timedelta

# ==========================================
# 環境変数設定 (Environment Variables Configuration)
# ==========================================
WEATHER_API_KEY = os.environ.get('WEATHER_API_KEY')
HOTPEPPER_API_KEY = os.environ.get('HOTPEPPER_API_KEY')

# DynamoDBリソースの初期化 (Initialization)
try:
    dynamodb = boto3.resource('dynamodb')
    table_name = 'OtenkiMeshi_Log'
    table = dynamodb.Table(table_name)
except Exception as e:
    print(f"DynamoDB Init Error: {e}")
    table = None

def get_weather_data(lat, lon):
    """
    OpenWeatherMap APIを使用して、現在地の天気・気温・湿度を取得する関数
    Returns:
        status (str): 天気状態 (例: Clear, Rain)
        temp (float): 気温 (摂氏)
        humidity (int): 湿度 (%)
    """
    try:
        url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&units=metric&appid={WEATHER_API_KEY}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as res:
            data = json.loads(res.read().decode())
            weather_id = data['weather'][0]['id']
            main_status = data['weather'][0]['main']
            temp = data['main']['temp']
            humidity = data['main']['humidity'] # 湿度情報を追加取得

            # 800番台(晴れ・曇り)の詳細な振り分け処理
            if weather_id == 800 or weather_id == 801:
                return "Clear", temp, humidity
            return main_status, temp, humidity
    except Exception as e:
        print(f"Weather API Error: {e}")
        traceback.print_exc() # CloudWatch Logsへ詳細エラーを出力
        return "Clear", 20.0, 50 # 取得失敗時のデフォルト値

def get_restaurants(lat, lon, keyword, search_range=3):
    """
    ホットペッパーグルメAPIを使用して、条件に合致するレストランを検索する関数
    Args:
        search_range (int): 検索範囲 1(300m) ~ 5(3000m)
    """
    try:
        base_url = "http://webservice.recruit.co.jp/hotpepper/gourmet/v1/"
        query_params = {
            'key': HOTPEPPER_API_KEY,
            'lat': lat,
            'lng': lon,
            'keyword': keyword,
            'range': search_range,
            'order': 4, # おススメ順 (Recommendation Order)
            'count': 50,
            'format': 'json',
        }
        encoded_params = urllib.parse.urlencode(query_params)
        full_url = f"{base_url}?{encoded_params}"
        
        req = urllib.request.Request(full_url)
        with urllib.request.urlopen(req) as res:
            data = json.loads(res.read().decode())
            
            # APIレスポンス構造の整合性チェック
            if 'results' in data and 'shop' in data['results']:
                shops = data['results']['shop']
                # 検索結果からランダムに最大5件を抽出
                return random.sample(shops, min(len(shops), 5))
            else:
                return []
    except Exception as e:
        print(f"HotPepper API Error: {e}")
        traceback.print_exc()
        return []

def save_log_to_dynamodb(lat, lon, weather, temp, keyword, logic):
    """
    ユーザーの検索ログをDynamoDBへ保存する関数（将来の分析およびサービス改善用）
    """
    if table is None: return

    try:
        JST = timezone(timedelta(hours=9))
        timestamp = datetime.now(JST).isoformat()
        req_id = str(uuid.uuid4())

        table.put_item(Item={
            'request_id': req_id,
            'timestamp': timestamp,
            'location': f"{lat},{lon}",
            'weather': weather,
            'temp': str(temp),
            'recommended_keyword': keyword,
            'logic_used': logic
        })
        print(f"Log saved successfully: {req_id}")
    except Exception as e:
        print(f"DynamoDB Write Error: {e}")
        # ログ保存の失敗がメイン機能（検索）に影響しないよう、例外を握りつぶして続行

def lambda_handler(event, context):
    """
    AWS Lambda メインハンドラー関数
    """
    # CORSヘッダー定義（クライアントサイドからのアクセスを許可）
    headers = {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type',
        'Access-Control-Allow-Methods': 'OPTIONS,GET'
    }

    try:
        # 1. リクエストパラメータの取得およびバリデーション
        params = event.get('queryStringParameters') or {}
        lat = params.get('lat')
        lon = params.get('lon')

        # 位置情報が欠落している場合、デフォルトとして新宿駅の座標を使用（フェイルセーフ）
        if not lat or not lon:
            lat = "35.690921"
            lon = "139.700258"

        # 2. 気象データの取得 (天気, 気温, 湿度)
        weather, temp, humidity = get_weather_data(lat, lon)
        
        # 3. 現在日時情報の取得 (JST)
        JST = timezone(timedelta(hours=9))
        now = datetime.now(JST)
        now_hour = now.hour
        weekday = now.weekday() # 0:月曜, 1:火曜 ... 4:金曜, ... 6:日曜

        # ==========================================
        # 4. レコメンドロジックの実行 (優先順位付きアルゴリズム)
        # ==========================================
        
        target_list = []
        logic_reason = ""
        search_range = 3 # デフォルト検索半径: 1000m

        # 条件判定フラグ
        is_bad_weather = weather in ['Rain', 'Snow', 'Thunderstorm', 'Drizzle']
        is_high_humidity = humidity >= 80 # 不快指数が高い場合
        is_extreme_hot = float(temp) >= 30.0
        is_extreme_cold = float(temp) <= 5.0

        # --- Priority 1: 極端な気象条件 (最優先) ---
        if is_bad_weather:
            target_list = [
                {"keyword": "駅近 ランチ", "msg": "雨に濡れにくい駅近のお店を厳選しました☔️"},
                {"keyword": "デリバリー", "msg": "雨足が強いですね。デリバリー対応店はいかがですか？"},
                {"keyword": "ちゃんぽん", "msg": "雨の日は温かいスープで体温を維持しましょう！"},
                {"keyword": "地下街", "msg": "地下街なら雨でも快適に移動できますよ。"}
            ]
            search_range = 2 # 半径500mに縮小
            logic_reason = f"Priority 1: Bad Weather ({weather})"

        elif is_extreme_hot:
            target_list = [
                {"keyword": "冷麺", "msg": "猛暑日です🥵 さっぱりした冷麺で涼みませんか？"},
                {"keyword": "かき氷", "msg": "危険な暑さです！かき氷でクールダウンしましょう🍧"},
                {"keyword": "うなぎ", "msg": "暑さに負けないよう、うなぎでスタミナ補給！"},
                {"keyword": "カフェ", "msg": "無理せず、涼しいカフェで休憩しましょう。"}
            ]
            search_range = 1 # 半径300m (暑いので移動距離を最小化)
            logic_reason = "Priority 1: Extreme Hot"

        elif is_extreme_cold:
            target_list = [
                {"keyword": "鍋", "msg": "極寒ですね🥶 鍋料理で芯から温まりましょう。"},
                {"keyword": "ラーメン", "msg": "寒い日は熱々の味噌ラーメンが体に染みます。"},
                {"keyword": "スープカレー", "msg": "スパイスの効果でポカポカになりましょう！"}
            ]
            search_range = 2
            logic_reason = "Priority 1: Extreme Cold"
            
        # --- Priority 2: 湿度・不快指数ロジック (蒸し暑い日など) ---
        elif is_high_humidity and float(temp) >= 25.0:
            target_list = [
                {"keyword": "冷やし中華", "msg": "ジメジメした日は、さっぱり冷やし中華が一番！"},
                {"keyword": "ソーダ", "msg": "湿度が高くて蒸しますね。炭酸でリフレッシュ！🥤"},
                {"keyword": "エスニック", "msg": "蒸し暑い日はスパイシーなエスニック料理が合います。"}
            ]
            logic_reason = "Priority 2: High Humidity"

        # --- Priority 3: 曜日・時間帯別ロジック (心理的要因へのアプローチ) ---
        else:
            # [A] 金曜日の夜 (花金・週末)
            if weekday == 4 and now_hour >= 18:
                target_list = [
                    {"keyword": "居酒屋", "msg": "一週間お疲れ様でした！花金はパーッといきましょう🍻"},
                    {"keyword": "焼き鳥", "msg": "ビールと焼き鳥、最高の組み合わせですね。"},
                    {"keyword": "バル", "msg": "今夜はちょっとおしゃれにバルで乾杯しませんか？🍷"}
                ]
                logic_reason = "Priority 3: Friday Night"

            # [B] 月曜日のランチ (憂鬱な気分を吹き飛ばす)
            elif weekday == 0 and 11 <= now_hour <= 14:
                 target_list = [
                    {"keyword": "激辛", "msg": "月曜日の憂鬱を吹き飛ばす！激辛料理はいかが？🌶️"},
                    {"keyword": "カツ丼", "msg": "今週も「勝つ」！カツ丼でエネルギーチャージ💪"},
                    {"keyword": "カレー", "msg": "スパイスの力で脳を活性化させましょう！"}
                 ]
                 logic_reason = "Priority 3: Monday Lunch"

            # [C] 通常のランチタイム (11:00 ~ 13:59)
            elif 11 <= now_hour <= 13:
                target_list = [
                    {"keyword": "定食", "msg": "今日のランチはバランスの良い定食で！🍱"},
                    {"keyword": "ハンバーガー", "msg": "天気も良いし、ガッツリハンバーガー！🍔"},
                    {"keyword": "パスタ", "msg": "午後の活力に、美味しいパスタランチ🍝"},
                    {"keyword": "オムライス", "msg": "ふわふわ卵のオムライスで幸せ気分。"},
                    {"keyword": "寿司ランチ", "msg": "たまには贅沢に寿司ランチなんてどう？🍣"}
                ]
                logic_reason = "Priority 3: Lunch Time"

            # [D] カフェ・軽食 (14:00 ~ 16:59)
            elif 14 <= now_hour <= 16:
                target_list = [
                    {"keyword": "パンケーキ", "msg": "午後のひとときに、甘いパンケーキ🥞"},
                    {"keyword": "カフェ", "msg": "コーヒーの香りでリラックスタイム☕️"},
                    {"keyword": "スイーツ", "msg": "疲れた頭には甘いスイーツが一番！🍰"},
                    {"keyword": "たこ焼き", "msg": "小腹満たしに熱々のたこ焼き！"}
                ]
                logic_reason = "Priority 3: Tea Time"

            # [E] ディナー (17:00 ~ 04:59)
            elif now_hour >= 17 or now_hour <= 4:
                target_list = [
                    {"keyword": "居酒屋", "msg": "今日もお疲れ様！近くで乾杯🍻"},
                    {"keyword": "焼き鳥", "msg": "香ばしい焼き鳥とビール、最高ですね。"},
                    {"keyword": "焼肉", "msg": "今日はガッツリ焼肉でスタミナ補給！🥩"},
                    {"keyword": "イタリアン", "msg": "おしゃれなバルでワインなんていかが？🍷"},
                    {"keyword": "餃子", "msg": "肉汁たっぷりの餃子でご飯が進む！🥟"}
                ]
                logic_reason = "Priority 3: Dinner Time"

            # [F] モーニング (05:00 ~ 10:59)
            else:
                target_list = [
                    {"keyword": "カフェ モーニング", "msg": "少し早起きして、優雅なモーニング☕️"},
                    {"keyword": "パン屋", "msg": "焼きたてのパンの香りで一日をスタート！🥐"},
                    {"keyword": "そば", "msg": "朝はササッと立ち食いそば！"},
                    {"keyword": "おにぎり", "msg": "日本の朝はやっぱりおにぎりとお味噌汁🍙"}
                ]
                logic_reason = "Priority 3: Morning"

        # 5. リストからランダムに1つ選択 (空の場合はデフォルトを設定)
        if not target_list:
            selected = {"keyword": "カフェ", "msg": "おすすめのお店を探してみました。"}
            logic_reason = "Fallback Default"
        else:
            selected = random.choice(target_list)
        
        # ==========================================
        # 6. 店舗検索実行および検索結果ゼロ件時のフォールバック処理
        # ==========================================
        
        # 初回検索実行
        shops = get_restaurants(lat, lon, selected['keyword'], search_range)

        # [再試行 1] 検索結果が0件の場合、範囲を広げて再検索 (1000m)
        if not shops and search_range < 3:
            print(f"Retry 1: Expanding range to 1000m for {selected['keyword']}")
            shops = get_restaurants(lat, lon, selected['keyword'], 3)
            logic_reason += " (Retry: Range Extended)"

        # [再試行 2: 最終手段] それでも0件の場合、汎用的なキーワードに変更して広域検索 (3000m)
        # ※ 地方や郊外で「該当なし」画面を出さないための安全策
        if not shops:
            print("Retry 2: No shops found. Switching to generic keyword.")
            
            # 時間帯に応じた汎用キーワードを設定
            if 5 <= now_hour < 11:
                fallback_keyword = "カフェ"
            elif 11 <= now_hour < 15:
                fallback_keyword = "ランチ"
            elif 15 <= now_hour < 17:
                fallback_keyword = "カフェ"
            else:
                fallback_keyword = "居酒屋" 

            # 半径3000m (Range 5) で広域検索を実行
            shops = get_restaurants(lat, lon, fallback_keyword, 5)
            
            # ユーザーへのメッセージも状況に合わせて更新
            selected['keyword'] = fallback_keyword
            selected['msg'] = "近くにピッタリのお店が見つからなかったので、周辺の人気スポットを探してきました！🏃‍♂️"
            logic_reason += f" (Final Fallback: {fallback_keyword} 3km)"
        
        # 7. 実行ログをDynamoDBへ保存
        save_log_to_dynamodb(lat, lon, weather, temp, selected['keyword'], logic_reason)

        # 8. クライアントへレスポンスを返却
        return {
            'statusCode': 200,
            'headers': headers,
            'body': json.dumps({
                'weather': weather,
                'temp': temp,
                'humidity': humidity, # 湿度情報もフロントエンドへ渡す
                'message': selected['msg'],
                'keyword': selected['keyword'],
                'shops': shops,
                'logic': logic_reason
            }, ensure_ascii=False)
        }
        
    except Exception as e:
        # ★ エラーハンドリング: 重大なエラーはCloudWatchへ記録し、クライアントには500を返す
        print("************ CRITICAL ERROR ************")
        print(f"Error Message: {str(e)}")
        traceback.print_exc() # スタックトレース出力

        return {
            'statusCode': 500,
            'headers': headers,
            'body': json.dumps({
                'error': 'Internal Server Error',
                'message': str(e)
            }, ensure_ascii=False)
        }