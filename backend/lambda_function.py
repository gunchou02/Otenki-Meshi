import json
import urllib.request
import urllib.parse
import os
import random
import uuid
import boto3
from datetime import datetime, timezone, timedelta

# ==========================================
# 環境変数設定 (Environment Variables)
# ==========================================
WEATHER_API_KEY = os.environ.get('WEATHER_API_KEY')
HOTPEPPER_API_KEY = os.environ.get('HOTPEPPER_API_KEY')

# ==========================================
# DynamoDB設定
# ==========================================
dynamodb = boto3.resource('dynamodb')
table_name = 'OtenkiMeshi_Log'
table = dynamodb.Table(table_name)

def get_weather_data(lat, lon):
    """
    OpenWeatherMap APIを使用して現在地の天気を取得
    """
    try:
        url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&units=metric&appid={WEATHER_API_KEY}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as res:
            data = json.loads(res.read().decode())
            weather_id = data['weather'][0]['id']
            main_status = data['weather'][0]['main']
            temp = data['main']['temp']

            # 800番台(晴れ・曇り)の詳細振り分け
            if weather_id == 800 or weather_id == 801:
                return "Clear", temp
            return main_status, temp
    except Exception as e:
        print(f"Weather API Error: {e}")
        # エラー時はデフォルト値を返却
        return "Clear", 20.0

def get_restaurants(lat, lon, keyword, search_range=3):
    """
    ホットペッパーグルメAPIを使用してレストランを検索
    range: 1(300m), 2(500m), 3(1000m), 4(2000m), 5(3000m)
    """
    try:
        base_url = "http://webservice.recruit.co.jp/hotpepper/gourmet/v1/"
        query_params = {
            'key': HOTPEPPER_API_KEY,
            'lat': lat,
            'lng': lon,
            'keyword': keyword,
            'range': search_range,
            'order': 4, # おススメ順
            'count': 50,
            'format': 'json',
        }
        encoded_params = urllib.parse.urlencode(query_params)
        full_url = f"{base_url}?{encoded_params}"
        
        req = urllib.request.Request(full_url)
        with urllib.request.urlopen(req) as res:
            data = json.loads(res.read().decode())
            shops = data['results']['shop']
            
            # ランダムに5件抽出
            if len(shops) >= 5:
                return random.sample(shops, 5)
            else:
                return shops
    except Exception as e:
        print(f"HotPepper API Error: {e}")
        return []

def save_log_to_dynamodb(lat, lon, weather, temp, keyword, logic):
    """
    検索ログをDynamoDBに保存（分析・改善用）
    """
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
        print(f"Log saved: {req_id}")
    except Exception as e:
        print(f"DynamoDB Error: {e}")

def lambda_handler(event, context):
    """
    メインハンドラー関数
    """
    try:
        # 1. パラメータ取得
        params = event.get('queryStringParameters') or {}
        lat = params.get('lat')
        lon = params.get('lon')

        # 位置情報がない場合は新宿駅をデフォルトに設定
        if not lat or not lon:
            lat = "35.690921"
            lon = "139.700258"

        # 2. 天気情報取得
        weather, temp = get_weather_data(lat, lon)
        
        # 3. 現在時刻取得 (JST)
        JST = timezone(timedelta(hours=9))
        now_hour = datetime.now(JST).hour

        # ==========================================
        # 4. レコメンドロジック (アルゴリズム改善版)
        # ==========================================
        
        target_list = []
        logic_reason = ""
        search_range = 3 # 基本検索半径: 1000m

        # 条件フラグ
        is_bad_weather = weather in ['Rain', 'Snow', 'Thunderstorm', 'Drizzle']
        is_extreme_hot = temp >= 30
        is_extreme_cold = temp <= 5

        # 悪天候・猛暑・極寒時は検索範囲を狭める (500m)
        if is_bad_weather or is_extreme_hot or is_extreme_cold:
            search_range = 2 
            
        # --- 時間帯別ロジック ---

        # [A] ランチタイム (11:00 ~ 13:59)
        if 11 <= now_hour <= 13:
            if is_bad_weather:
                target_list = [
                    {"keyword": "ちゃんぽん", "msg": "雨の日は温かいちゃんぽんで温まりましょう！"},
                    {"keyword": "うどん", "msg": "近場のうどんでサクッとランチはいかが？"},
                    {"keyword": "駅近 ランチ", "msg": "雨に濡れにくい駅近のお店を探しました。"},
                    {"keyword": "カレー", "msg": "ジメジメした天気にはスパイシーなカレー！"}
                ]
                logic_reason = "Lunch (Bad Weather)"
            else:
                target_list = [
                    {"keyword": "定食", "msg": "今日のランチはバランスの良い定食で！"},
                    {"keyword": "ハンバーガー", "msg": "天気も良いし、ガッツリハンバーガー！"},
                    {"keyword": "パスタ", "msg": "午後の活力に、美味しいパスタランチ。"},
                    {"keyword": "寿司ランチ", "msg": "たまには贅沢に寿司ランチなんてどう？"},
                    {"keyword": "オムライス", "msg": "ふわふわ卵のオムライスで幸せ気分。"}
                ]
                logic_reason = "Lunch (Good Weather)"

        # [B] カフェ・軽食タイム (14:00 ~ 16:59)
        # ※以前の「カフェばかり出る」問題を解消するためメニューを多様化
        elif 14 <= now_hour <= 16:
            target_list = [
                {"keyword": "パンケーキ", "msg": "午後のひとときに、甘いパンケーキはいかが？"},
                {"keyword": "ハンバーガー", "msg": "小腹が空いたらハンバーガー！"},
                {"keyword": "タピオカ", "msg": "糖分補給にタピオカドリンク！"},
                {"keyword": "たこ焼き", "msg": "おやつに熱々のたこ焼きはいかが？"},
                {"keyword": "カフェ", "msg": "カフェでまったり読書でもしながら休憩。"}, # 確率は下がる
                {"keyword": "スイーツ", "msg": "疲れた頭には甘いスイーツが一番！"}
            ]
            logic_reason = "Afternoon Tea/Snack"

        # [C] ディナー・夜食 (17:00 ~ 04:59)
        elif now_hour >= 17 or now_hour <= 4:
            if is_bad_weather:
                target_list = [
                    {"keyword": "鍋", "msg": "外は悪天候。温かい鍋で温まりましょう。"},
                    {"keyword": "おでん", "msg": "雨の夜はしっぽりおでんで一杯。"},
                    {"keyword": "個室 居酒屋", "msg": "雨宿りついでに、個室居酒屋でゆっくり。"}
                ]
                logic_reason = "Dinner (Bad Weather)"
            else:
                target_list = [
                    {"keyword": "居酒屋", "msg": "今日もお疲れ様！近くの居酒屋で乾杯！"},
                    {"keyword": "焼き鳥", "msg": "仕事帰りに焼き鳥とビール、最高ですね。"},
                    {"keyword": "焼肉", "msg": "今日はガッツリ焼肉でスタミナ補給！"},
                    {"keyword": "バル", "msg": "おしゃれなバルでワインなんていかが？"},
                    {"keyword": "餃子", "msg": "ビールと餃子の最強コンビで決まり！"}
                ]
                logic_reason = "Dinner (Good Weather)"

        # [D] モーニング (05:00 ~ 10:59)
        elif 5 <= now_hour <= 10:
            target_list = [
                {"keyword": "カフェ モーニング", "msg": "少し早起きして、カフェでモーニング。"},
                {"keyword": "パン屋", "msg": "焼きたてのパンの香りで一日をスタート！"},
                {"keyword": "そば", "msg": "朝はササッと立ち食いそば！"},
                {"keyword": "おにぎり", "msg": "日本の朝はやっぱりおにぎりとお味噌汁。"}
            ]
            logic_reason = "Morning"
            
        # [E] その他・例外処理 (気温ベースの判定)
        else:
            if is_extreme_hot:
                target_list = [
                    {"keyword": "かき氷", "msg": "暑すぎます！かき氷でクールダウン。"},
                    {"keyword": "冷麺", "msg": "食欲がない時はさっぱり冷麺！"},
                    {"keyword": "アイス", "msg": "暑いのでアイスクリーム食べに行きませんか？"},
                    {"keyword": "カフェ", "msg": "涼しいカフェに避難しましょう。"}
                ]
                search_range = 1 
                logic_reason = "Hot Weather"
            elif is_extreme_cold:
                target_list = [
                    {"keyword": "ラーメン", "msg": "寒い日は味噌ラーメンが染みる…"},
                    {"keyword": "スープカレー", "msg": "スパイスで体の中から温まるスープカレー。"},
                    {"keyword": "鍋", "msg": "鍋料理が恋しい季節ですね。"}
                ]
                search_range = 2
                logic_reason = "Cold Weather"
            else:
                # 天気別デフォルトメニュー
                weather_menus = {
                    "Rain": [
                        {"keyword": "ラーメン", "msg": "雨の日はラーメン率高め？"},
                        {"keyword": "デリバリー", "msg": "雨だし、デリバリー対応のお店を探すのもアリ。"}
                    ],
                    "Clear": [
                        {"keyword": "サンドイッチ", "msg": "天気がいいのでサンドイッチを買って公園へ！"},
                        {"keyword": "ハンバーガー", "msg": "晴れた日はジャンクフードが美味しい。"},
                        {"keyword": "カフェ テラス", "msg": "テラス席のあるカフェで光合成しましょう。"}
                    ],
                    "Clouds": [
                        {"keyword": "中華", "msg": "曇り空も吹き飛ばす、熱々の中華！"},
                        {"keyword": "定食", "msg": "迷ったら安定の定食屋さんへ。"}
                    ]
                }
                # 完全なデフォルト
                default_opt = [
                    {"keyword": "カフェ", "msg": "ちょっと一息つきましょう。"},
                    {"keyword": "ファミレス", "msg": "ドリンクバーでゆっくり作戦会議。"},
                    {"keyword": "パン屋", "msg": "小腹が空いたらパン屋さんへ。"}
                ]
                target_list = weather_menus.get(weather, default_opt)
                logic_reason = f"Weather Only: {weather}"

        # 5. リストからランダムに1つ選択
        selected = random.choice(target_list)
        
        # 6. 店舗検索実行
        shops = get_restaurants(lat, lon, selected['keyword'], search_range)

        # 検索結果が0件の場合、範囲を広げて再検索 (最大1000m)
        if not shops and search_range < 3:
            shops = get_restaurants(lat, lon, selected['keyword'], 3)
            logic_reason += " (Retry with Expanded Range)"
        
        # 7. ログ保存
        save_log_to_dynamodb(lat, lon, weather, temp, selected['keyword'], logic_reason)

        # 8. レスポンス返却
        return {
            'statusCode': 200,
            'headers': {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Headers': 'Content-Type',
                'Access-Control-Allow-Methods': 'OPTIONS,GET'
            },
            'body': json.dumps({
                'weather': weather,
                'temp': temp,
                'message': selected['msg'],
                'keyword': selected['keyword'],
                'shops': shops,
                'logic': logic_reason
            }, ensure_ascii=False)
        }
        
    except Exception as e:
        return {
            'statusCode': 500,
            'body': json.dumps(f"Server Error: {str(e)}")
        }