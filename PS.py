import datetime
import glob
import json
import math
import os
import sqlite3

import folium
import polyline
import requests
import streamlit as st
from shapely.geometry import Point, shape
from streamlit_folium import st_folium

# ---------------------------------------------------------
# パスワード認証処理
# ---------------------------------------------------------
def check_password():
    if "PASSWORD" not in st.secrets:
        return True

    def password_entered():
        if st.session_state["password_input"] == st.secrets["PASSWORD"]:
            st.session_state["password_correct"] = True
            del st.session_state["password_input"]
        else:
            st.session_state["password_correct"] = False

    if st.session_state.get("password_correct", False):
        return True

    st.title("🔒 ログインが必要です")
    st.text_input(
        "パスワードを入力してください",
        type="password",
        on_change=password_entered,
        key="password_input"
    )

    if "password_correct" in st.session_state and not st.session_state["password_correct"]:
        st.error("パスワードが違います。")

    return False

if not check_password():
    st.stop()

# ---------------------------------------------------------
# API Key & 定数設定
# ---------------------------------------------------------
GOOGLE_MAPS_API_KEY = st.secrets.get("GOOGLE_MAPS_API_KEY", "YOUR_GOOGLE_API_KEY")
HERE_API_KEY = st.secrets.get("HERE_API_KEY", "YOUR_HERE_API_KEY")

PICKUP_FEE = 300      # 迎車料金 (1乗車につき固定)
RESERVATION_FEE = 500 # 予約料金 (選択時)
BRIDGE_FEE = 910      # 橋代往復加算料金

DAILY_GLOBAL_LIMIT = 300
DB_FILE = "usage_counter.db"

# ---------------------------------------------------------
# SQLite 利用回数カウント関数
# ---------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_usage (
            date TEXT PRIMARY KEY,
            count INTEGER
        )
    """)
    conn.commit()
    conn.close()

def get_today_usage():
    init_db()
    today = datetime.date.today().isoformat()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT count FROM daily_usage WHERE date = ?", (today,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def increment_today_usage():
    init_db()
    today = datetime.date.today().isoformat()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO daily_usage (date, count)
        VALUES (?, 1)
        ON CONFLICT(date) DO UPDATE SET count = count + 1
    """, (today,))
    conn.commit()
    conn.close()

# ---------------------------------------------------------
# Session State 初期化
# ---------------------------------------------------------
if "start_point_val" not in st.session_state:
    st.session_state["start_point_val"] = ""
if "end_point_val" not in st.session_state:
    st.session_state["end_point_val"] = ""

if "via_list" not in st.session_state:
    st.session_state["via_list"] = []

if "start_coords" not in st.session_state:
    st.session_state["start_coords"] = None
if "end_coords" not in st.session_state:
    st.session_state["end_coords"] = None

if "last_processed_click" not in st.session_state:
    st.session_state["last_processed_click"] = None

# ---------------------------------------------------------
# GeoJSON読み込み & エリア判定関数
# ---------------------------------------------------------
@st.cache_data
def load_all_area_geojsons():
    """青い枠（営業エリア）のGeoJSON群を読み込み"""
    all_features = []
    base_dir = os.path.dirname(os.path.abspath(__file__))
    search_pattern = os.path.join(base_dir, "area_*.geojson")
    geojson_files = glob.glob(search_pattern)

    for file_path in geojson_files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                all_features.extend(data.get("features", []))
        except Exception as e:
            st.error(f"{os.path.basename(file_path)} の読み込みに失敗: {e}")

    return all_features

@st.cache_data
def load_one_way_area_geojson():
    """赤い枠（高速料金が片道で済む境界エリア）のGeoJSONを読み込み"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(base_dir, "one_way_area.geojson")

    if not os.path.exists(file_path):
        return None

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("features", [])
    except Exception as e:
        st.error(f"one_way_area.geojson の読み込みに失敗: {e}")
        return None

@st.cache_data
def load_bridge_area_geojson():
    """橋代往復エリア（+910円加算）のGeoJSONを読み込み"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(base_dir, "bridge_area.geojson")

    if not os.path.exists(file_path):
        return None

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("features", [])
    except Exception as e:
        st.error(f"bridge_area.geojson の読み込みに失敗: {e}")
        return None

ALL_FEATURES = load_all_area_geojsons()
ONE_WAY_FEATURES = load_one_way_area_geojson()
BRIDGE_FEATURES = load_bridge_area_geojson()

def find_area(lat, lon):
    """指定座標が属するタクシー運賃エリア（area_*.geojson）を取得"""
    if lat is None or lon is None or not ALL_FEATURES:
        return None

    point = Point(lon, lat)

    for feature in ALL_FEATURES:
        polygon = shape(feature["geometry"])
        if polygon.intersects(point) or polygon.covers(point) or polygon.contains(point):
            props = feature["properties"]
            return {
                "name": props.get("name", "名称未設定"),
                "base_fare": int(props.get("base_fare", 500)),
                "base_distance_m": int(props.get("base_distance_m", 1000)),
                "add_fare": int(props.get("add_fare", 100)),
                "add_distance_m": int(props.get("add_distance_m", 250))
            }
    return None

def is_in_one_way_area(lat, lon):
    if lat is None or lon is None or not ONE_WAY_FEATURES:
        return False

    point = Point(lon, lat)
    for feature in ONE_WAY_FEATURES:
        polygon = shape(feature["geometry"])
        if polygon.intersects(point) or polygon.covers(point):
            return True
    return False

def is_in_bridge_area(lat, lon):
    if lat is None or lon is None or not BRIDGE_FEATURES:
        return False

    point = Point(lon, lat)
    for feature in BRIDGE_FEATURES:
        polygon = shape(feature["geometry"])
        if polygon.intersects(point) or polygon.covers(point):
            return True
    return False

# ---------------------------------------------------------
# Google Maps API 関数
# ---------------------------------------------------------
def get_coordinates_google(address):
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address, "key": GOOGLE_MAPS_API_KEY, "language": "ja"}
    try:
        res = requests.get(url, params=params).json()
        if res.get("status") == "OK" and len(res["results"]) > 0:
            loc = res["results"][0]["geometry"]["location"]
            return loc["lat"], loc["lng"]
        st.error(f"Google ジオコーディング検索失敗 ({address})")
        return None, None
    except Exception as e:
        st.error(f"Google Geocoding API エラー: {e}")
        return None, None

def reverse_geocode_google(lat, lon):
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"latlng": f"{lat},{lon}", "key": GOOGLE_MAPS_API_KEY, "language": "ja"}
    try:
        res = requests.get(url, params=params).json()
        if res.get("status") == "OK" and len(res["results"]) > 0:
            return res["results"][0]["formatted_address"]
        return f"{lat:.5f}, {lon:.5f}"
    except Exception:
        return f"{lat:.5f}, {lon:.5f}"

def get_google_route(origin_lat, origin_lon, dest_lat, dest_lon, avoid_highways=False):
    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": f"{origin_lat},{origin_lon}",
        "destination": f"{dest_lat},{dest_lon}",
        "mode": "driving",
        "key": GOOGLE_MAPS_API_KEY,
        "language": "ja"
    }
    if avoid_highways:
        params["avoid"] = "tolls"

    try:
        res = requests.get(url, params=params).json()
        if res.get("status") != "OK" or not res.get("routes"):
            st.error("Google Directions API でルートが見つかりませんでした。")
            return None

        route = res["routes"][0]
        leg = route["legs"][0]
        distance_km = leg["distance"]["value"] / 1000.0
        path_coords = polyline.decode(route["overview_polyline"]["points"])

        return {
            "distance_km": distance_km,
            "path_coords": path_coords
        }
    except Exception as e:
        st.error(f"Google Directions API 通信エラー: {e}")
        return None

# ---------------------------------------------------------
# HERE API 関数
# ---------------------------------------------------------
def get_here_toll_fee_full_route(origin_lat, origin_lon, dest_lat, dest_lon, via_coords_list=None, avoid_highways=False):
    if avoid_highways:
        return 0

    url = "https://router.hereapi.com/v8/routes"
    params = {
        "transportMode": "car",
        "origin": f"{origin_lat},{origin_lon}",
        "destination": f"{dest_lat},{dest_lon}",
        "return": "tolls",
        "tolls[transponders]": "all",
        "routingMode": "fast",
        "apiKey": HERE_API_KEY,
        "lang": "ja"
    }

    if via_coords_list and len(via_coords_list) > 0:
        limited_vias = via_coords_list[:5]
        v_param = [f"{lat},{lon}" for lat, lon in limited_vias]
        params["via"] = v_param

    try:
        res = requests.get(url, params=params).json()
        if "routes" not in res or len(res["routes"]) == 0:
            return 0

        total_toll_cost = 0
        sections = res["routes"][0].get("sections", [])
        
        for section in sections:
            if "tolls" in section:
                for toll in section["tolls"]:
                    for fare in toll.get("fares", []):
                        price_val = fare.get("price", {}).get("value", 0)
                        if price_val > 0:
                            total_toll_cost += int(price_val)
                            
        return total_toll_cost
    except Exception:
        return 0

# ---------------------------------------------------------
# タクシー料金計算ロジック
# ---------------------------------------------------------
def calculate_segment_fare(distance_km, rule, is_night):
    if distance_km is None or distance_km == 0:
        return 0

    distance_m = distance_km * 1000.0
    base_fare = rule["base_fare"]
    base_dist = rule["base_distance_m"]
    add_fare = rule["add_fare"]
    add_dist = rule["add_distance_m"]

    if distance_m <= base_dist:
        raw_fare = base_fare
    else:
        extra_dist = distance_m - base_dist
        steps = math.ceil(extra_dist / add_dist)
        
        current_fare = base_fare
        discounted_extra_fare = 0
        
        for _ in range(steps):
            if current_fare < 5000:
                current_fare += add_fare
            else:
                discounted_extra_fare += (add_fare * 0.7)
                
        raw_fare = current_fare if current_fare < 5000 else 5000 + discounted_extra_fare

    if is_night:
        raw_fare *= 1.2

    total_segment_fare = raw_fare + PICKUP_FEE
    return int(math.ceil(total_segment_fare / 10) * 10)

# ---------------------------------------------------------
# Folium 地図描画処理
# ---------------------------------------------------------
def draw_map(points_markers=None, all_path_coords=None):
    if points_markers and len(points_markers) > 0:
        avg_lat = sum(p[0] for p in points_markers) / len(points_markers)
        avg_lon = sum(p[1] for p in points_markers) / len(points_markers)
        center = [avg_lat, avg_lon]
        zoom = 11
    else:
        center = [34.7024, 135.4959]
        zoom = 11

    m = folium.Map(location=center, zoom_start=zoom, tiles=None)

    folium.TileLayer(
        tiles="https://mt1.google.com/vt/lyrs=m&x={x}&y={y}&z={z}",
        attr="Google Maps",
        name="Google マップ",
        overlay=False,
        control=True
    ).add_to(m)

    if ALL_FEATURES:
        geojson_data = {"type": "FeatureCollection", "features": ALL_FEATURES}
        folium.GeoJson(
            geojson_data,
            name="タクシーエリアポリゴン",
            style_function=lambda feature: {
                "fillColor": "#3186cc",
                "color": "#2b5c8f",
                "weight": 2,
                "fillOpacity": 0.2,
            },
            tooltip=folium.GeoJsonTooltip(fields=["name"], aliases=["エリア名:"]),
            interactive=False
        ).add_to(m)

    if points_markers:
        for lat, lon, label, color in points_markers:
            folium.Marker(
                location=[lat, lon],
                popup=label,
                tooltip=label,
                icon=folium.Icon(color=color, icon="info-sign")
            ).add_to(m)

    if all_path_coords:
        for path in all_path_coords:
            folium.PolyLine(locations=path, color="blue", weight=5, opacity=0.7).add_to(m)

    folium.LayerControl().add_to(m)
    return m

# ---------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------
st.title("MKタクシー料金計算アプリ")

col1, col2 = st.columns(2)
with col1:
    start_point = st.text_input("始点（出発地）", value=st.session_state["start_point_val"])
    st.session_state["start_point_val"] = start_point

with col2:
    end_point = st.text_input("終点（目的地）", value=st.session_state["end_point_val"])
    st.session_state["end_point_val"] = end_point

st.markdown("### 経由地設定（最大3件）")
col_b1, col_b2, _ = st.columns([1, 1, 2])
with col_b1:
    if len(st.session_state["via_list"]) < 3:
        if st.button("➕ 経由地を追加する"):
            st.session_state["via_list"].append({"address": "", "reset_meter": False, "coords": None})
            st.rerun()
with col_b2:
    if len(st.session_state["via_list"]) > 0:
        if st.button("🗑️ 経由地を減らす"):
            st.session_state["via_list"].pop()
            st.rerun()

for idx, via_item in enumerate(st.session_state["via_list"]):
    col_v1, col_v2 = st.columns([2, 1])
    with col_v1:
        v_address = st.text_input(
            f"経由地 {idx + 1} の場所",
            value=via_item["address"],
            key=f"via_address_{idx}"
        )
        st.session_state["via_list"][idx]["address"] = v_address
    with col_v2:
        st.write("")
        st.write("")
        v_reset = st.checkbox(
            f"経由地 {idx + 1} でメーター切り直し",
            value=via_item["reset_meter"],
            key=f"via_reset_{idx}"
        )
        st.session_state["via_list"][idx]["reset_meter"] = v_reset

st.markdown("### 料金オプション設定")
col_opt1, col_opt2, col_opt3 = st.columns(3)
with col_opt1:
    use_reservation = st.checkbox("予約を行う (+500円)")
with col_opt2:
    is_night = st.checkbox("深夜割増 (22:00〜5:00 / 2割増)")
with col_opt3:
    use_highway = st.checkbox("有料・高速道路を利用する", value=True)

manual_toll_fee = 0
if use_highway:
    default_toll = 0
    if "calc_result" in st.session_state and not st.session_state["calc_result"].get("error"):
        default_toll = st.session_state["calc_result"].get("total_toll_fee", 0)
        
    manual_toll_fee = st.number_input(
        "高速・有料道路料金（円） ※手動修正・直接入力用",
        min_value=0,
        value=default_toll,
        step=100
    )

st.markdown("---")
st.markdown("### 🗺️ マップ (クリックして地点を設定)")

click_target_options = ["始点に設定"]
for idx in range(len(st.session_state["via_list"])):
    click_target_options.append(f"経由地{idx + 1}に設定")
click_target_options.append("終点に設定")

click_target = st.radio("👇 地図上でクリックした位置の割り当て先を選択してください:", click_target_options, horizontal=True)

current_markers = []
if st.session_state["start_coords"]:
    lat, lon = st.session_state["start_coords"]
    current_markers.append((lat, lon, f"始点: {st.session_state['start_point_val']}", "green"))

for idx, via_item in enumerate(st.session_state["via_list"]):
    if via_item.get("coords"):
        lat, lon = via_item["coords"]
        current_markers.append((lat, lon, f"経由地{idx + 1}: {via_item['address']}", "orange"))

if st.session_state["end_coords"]:
    lat, lon = st.session_state["end_coords"]
    current_markers.append((lat, lon, f"終点: {st.session_state['end_point_val']}", "red"))

prev_paths = None
if "calc_result" in st.session_state and not st.session_state["calc_result"].get("error"):
    prev_paths = st.session_state["calc_result"].get("all_path_coords")

map_obj = draw_map(current_markers, prev_paths)
map_data = st_folium(map_obj, width=700, height=450, key="map_component")

clicked_point = None
if map_data:
    if map_data.get("last_clicked"):
        clicked_point = map_data["last_clicked"]
    elif map_data.get("last_geojson_clicked") and "geometry" in map_data["last_geojson_clicked"]:
        coords = map_data["last_geojson_clicked"]["geometry"]["coordinates"]
        if isinstance(coords, list) and len(coords) >= 2:
            clicked_point = {"lat": coords[1], "lng": coords[0]}

if clicked_point:
    clicked_lat = clicked_point["lat"]
    clicked_lng = clicked_point["lng"]
    current_click_key = f"{clicked_lat:.5f},{clicked_lng:.5f}"

    if st.session_state["last_processed_click"] != current_click_key:
        st.session_state["last_processed_click"] = current_click_key

        if GOOGLE_MAPS_API_KEY and GOOGLE_MAPS_API_KEY != "YOUR_GOOGLE_API_KEY":
            with st.spinner("Google Maps から住所を取得中..."):
                address = reverse_geocode_google(clicked_lat, clicked_lng)
                
                if click_target == "始点に設定":
                    st.session_state["start_point_val"] = address
                    st.session_state["start_coords"] = (clicked_lat, clicked_lng)
                elif click_target == "終点に設定":
                    st.session_state["end_point_val"] = address
                    st.session_state["end_coords"] = (clicked_lat, clicked_lng)
                else:
                    for idx in range(len(st.session_state["via_list"])):
                        if click_target == f"経由地{idx + 1}に設定":
                            st.session_state["via_list"][idx]["address"] = address
                            st.session_state["via_list"][idx]["coords"] = (clicked_lat, clicked_lng)
                
                st.rerun()

# ---------------------------------------------------------
# 料金計算処理
# ---------------------------------------------------------
st.markdown("---")

today_count = get_today_usage()
remaining = DAILY_GLOBAL_LIMIT - today_count
is_disabled = today_count >= DAILY_GLOBAL_LIMIT

st.caption(f"💡 本日の全体計算状況: あと **{max(0, remaining)}** / {DAILY_GLOBAL_LIMIT} 回 計算可能です")

if is_disabled:
    st.error("⚠️ 本日の全体利用上限（300回）に達しました。明日（0時以降）に再度お試しください。")

if st.button("料金とルートを計算する", type="primary", disabled=is_disabled):
    if GOOGLE_MAPS_API_KEY == "YOUR_GOOGLE_API_KEY" or not GOOGLE_MAPS_API_KEY:
        st.error("API Key が設定されていません。")
    elif not start_point or not end_point:
        st.warning("始点と終点を入力してください。")
    elif any(not v["address"] for v in st.session_state["via_list"]):
        st.warning("入力されていない経由地があります。")
    else:
        increment_today_usage()

        with st.spinner("Google Routes と HERE 高速料金を計算中..."):
            avoid_highways = not use_highway
            points = []
            
            # 1. 始点
            s_lat, s_lon = get_coordinates_google(start_point)
            if s_lat is None:
                st.error("始点の位置情報が取得できませんでした。")
                st.stop()
            st.session_state["start_coords"] = (s_lat, s_lon)
            points.append({"name": start_point, "lat": s_lat, "lon": s_lon, "reset_after": False, "type": "start"})

            # 2. 経由地
            via_error = False
            for idx, via_item in enumerate(st.session_state["via_list"]):
                v_lat, v_lon = get_coordinates_google(via_item["address"])
                if v_lat is None:
                    st.error(f"経由地 {idx + 1} の位置情報が取得できませんでした。")
                    via_error = True
                    break
                st.session_state["via_list"][idx]["coords"] = (v_lat, v_lon)
                points.append({
                    "name": via_item["address"],
                    "lat": v_lat,
                    "lon": v_lon,
                    "reset_after": via_item["reset_meter"],
                    "type": "via"
                })

            if via_error:
                st.stop()

            # 3. 終点
            e_lat, e_lon = get_coordinates_google(end_point)
            if e_lat is None:
                st.error("終点の位置情報が取得できませんでした。")
                st.stop()
            st.session_state["end_coords"] = (e_lat, e_lon)
            points.append({"name": end_point, "lat": e_lat, "lon": e_lon, "reset_after": False, "type": "end"})

            # 各地点ごとの営業エリア（area_*.geojson）判定
            for pt in points:
                pt["area"] = find_area(pt["lat"], pt["lon"])

            # 💡 全体の「最初の始点」と「最後の終点」の両方がエリア外（None）の時のみ即エラーブロック
            first_start_area = points[0]["area"]
            last_end_area = points[-1]["area"]
            
            if first_start_area is None and last_end_area is None:
                st.session_state["calc_result"] = {
                    "error": True,
                    "error_message": "始点および終点の両方が営業エリア外のため、料金計算を行えません。"
                }
                st.rerun()

            # 片道/往復の境界エリア（one_way_area.geojson）判定
            start_in_one_way = is_in_one_way_area(points[0]["lat"], points[0]["lon"])
            end_in_one_way = is_in_one_way_area(points[-1]["lat"], points[-1]["lon"])

            # メーター切り直し区間の分割
            meter_segments = []
            current_segment_pts = [points[0]]

            for i in range(1, len(points)):
                current_segment_pts.append(points[i])
                if points[i - 1]["reset_after"] or i == len(points) - 1:
                    meter_segments.append(current_segment_pts)
                    if i < len(points) - 1:
                        current_segment_pts = [points[i]]

            all_path_coords = []
            total_distance = 0.0
            taxi_fare = 0
            error_flag = False
            error_message = ""
            info_messages = []
            caption_messages = []
            here_via_coords = []

            # 信頼できる適用エリアのフォールバック（手配全地点の中で最初に見つかった有効なエリア）
            global_fallback_area = next((p["area"] for p in points if p["area"] is not None), None)

            for seg_idx, seg_pts in enumerate(meter_segments):
                seg_start = seg_pts[0]
                seg_end = seg_pts[-1]
                
                # 区間内の始点エリア ➔ 区間内の終点エリア ➔ 手配全体の有効エリア の順で確実に取得
                applied_rule = seg_start.get("area") or seg_end.get("area") or global_fallback_area

                seg_dist = 0.0
                for k in range(len(seg_pts) - 1):
                    p1 = seg_pts[k]
                    p2 = seg_pts[k + 1]

                    route_info = get_google_route(p1["lat"], p1["lon"], p2["lat"], p2["lon"], avoid_highways)
                    if route_info is None:
                        error_flag = True
                        break

                    seg_dist += route_info["distance_km"]
                    all_path_coords.append(route_info["path_coords"])

                    if k < len(seg_pts) - 2:
                        here_via_coords.append((p2["lat"], p2["lon"]))

                if error_flag:
                    break

                seg_fare = calculate_segment_fare(seg_dist, applied_rule, is_night)
                
                total_distance += seg_dist
                taxi_fare += seg_fare

                if total_distance > 300.0:
                    error_flag = True
                    error_message = f"走行距離が 300km ({total_distance:.1f}km) を超えているため、計算できません。"
                    break

                if len(meter_segments) > 1:
                    info_messages.append(f"区間 {seg_idx + 1} ({seg_start['name']} ➔ {seg_end['name']}) 適用エリア: **{applied_rule['name']}**")
                    caption_messages.append(f"・区間 {seg_idx + 1}: {seg_dist:.2f} km / {seg_fare:,} 円 (迎車込)")
                else:
                    info_messages.append(f"適用運賃エリア: **{applied_rule['name']}**")

            if error_flag:
                st.session_state["calc_result"] = {
                    "error": True,
                    "error_message": error_message if error_message else "ルート検索に失敗しました。"
                }
            else:
                # 高速料金算出
                raw_toll = get_here_toll_fee_full_route(
                    points[0]["lat"], points[0]["lon"],
                    points[-1]["lat"], points[-1]["lon"],
                    via_coords_list=here_via_coords,
                    avoid_highways=avoid_highways
                )

                is_round_trip = start_in_one_way != end_in_one_way
                api_toll_fee = raw_toll * 2 if is_round_trip else raw_toll

                start_in_bridge = is_in_bridge_area(points[0]["lat"], points[0]["lon"])
                end_in_bridge = is_in_bridge_area(points[-1]["lat"], points[-1]["lon"])
                has_bridge_fee = (start_in_bridge or end_in_bridge) and use_highway

                if has_bridge_fee:
                    api_toll_fee += BRIDGE_FEE

                final_toll_fee = api_toll_fee if api_toll_fee > 0 else manual_toll_fee
                res_fee = RESERVATION_FEE if use_reservation else 0
                grand_total = taxi_fare + res_fee + final_toll_fee

                st.session_state["calc_result"] = {
                    "error": False,
                    "total_distance": total_distance,
                    "taxi_fare": taxi_fare,
                    "grand_total": grand_total,
                    "use_reservation": use_reservation,
                    "total_toll_fee": final_toll_fee,
                    "is_round_trip": is_round_trip,
                    "has_bridge_fee": has_bridge_fee,
                    "info_messages": info_messages,
                    "caption_messages": caption_messages,
                    "all_path_coords": all_path_coords
                }
                st.rerun()

# ---------------------------------------------------------
# 結果表示
# ---------------------------------------------------------
if "calc_result" in st.session_state:
    res = st.session_state["calc_result"]
    if res.get("error"):
        st.error(f"⛔ {res.get('error_message', '計算に失敗しました。')}")
    else:
        st.success("計算が完了しました！")
        
        for msg in res["info_messages"]:
            st.info(msg)
        for msg in res["caption_messages"]:
            st.caption(msg)

        has_toll = res["total_toll_fee"] > 0
        
        toll_label = "高速料金のみ (往復エリア)" if res.get("is_round_trip", False) else "高速料金のみ (ETC)"
        if res.get("has_bridge_fee", False):
            toll_label += " ※橋代+910円込"

        if has_toll:
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                st.metric("総実走行距離", f"{res['total_distance']:.2f} km")
            with c2:
                st.metric("タクシー運賃 (迎車込)", f"{res['taxi_fare']:,} 円")
            with c3:
                st.metric(toll_label, f"{res['total_toll_fee']:,} 円")
            with c4:
                st.metric("支払総額 (合計)", f"{res['grand_total']:,} 円")
        else:
            c1, c2, c3 = st.columns(3)
            with c1:
                st.metric("総実走行距離", f"{res['total_distance']:.2f} km")
            with c2:
                st.metric("タクシー運賃 (迎車込)", f"{res['taxi_fare']:,} 円")
            with c3:
                st.metric("支払総額 (合計)", f"{res['grand_total']:,} 円")

        details = [f"タクシー運賃(迎車込): {res['taxi_fare']:,}円"]
        if res["use_reservation"]:
            details.append(f"予約料金: {RESERVATION_FEE}円")
        if has_toll:
            details.append(f"{toll_label}: {res['total_toll_fee']:,}円")
        
        st.markdown(f"**【金額内訳】** {' + '.join(details)} ＝ **合計 {res['grand_total']:,}円**")