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

# API Key & 定数設定
GOOGLE_MAPS_API_KEY = st.secrets.get("GOOGLE_MAPS_API_KEY", "YOUR_GOOGLE_API_KEY")
HERE_API_KEY = st.secrets.get("HERE_API_KEY", "YOUR_HERE_API_KEY")
PICKUP_FEE = 300
RESERVATION_FEE = 500
BRIDGE_FEE = 910
DAILY_GLOBAL_LIMIT = 300
DB_FILE = "usage_counter.db"

# セッション初期化
if "via_list" not in st.session_state:
    st.session_state["via_list"] = []

@st.cache_data
def load_all_area_geojsons():
    all_features = []
    base_dir = os.path.dirname(os.path.abspath(__file__))
    search_pattern = os.path.join(base_dir, "area_*.geojson")
    geojson_files = glob.glob(search_pattern)
    for file_path in geojson_files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                all_features.extend(data.get("features", []))
        except Exception:
            pass
    return all_features

ALL_FEATURES = load_all_area_geojsons()

def find_area(lat, lon):
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

def get_coordinates_google(address):
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address, "key": GOOGLE_MAPS_API_KEY, "language": "ja"}
    try:
        res = requests.get(url, params=params).json()
        if res.get("status") == "OK" and len(res["results"]) > 0:
            loc = res["results"][0]["geometry"]["location"]
            return loc["lat"], loc["lng"]
        return None, None
    except Exception:
        return None, None

def get_google_route(origin_lat, origin_lon, dest_lat, dest_lon):
    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": f"{origin_lat},{origin_lon}",
        "destination": f"{dest_lat},{dest_lon}",
        "mode": "driving",
        "key": GOOGLE_MAPS_API_KEY,
        "language": "ja"
    }
    try:
        res = requests.get(url, params=params).json()
        if res.get("status") != "OK" or not res.get("routes"):
            return None
        route = res["routes"][0]
        leg = route["legs"][0]
        return leg["distance"]["value"] / 1000.0
    except Exception:
        return None

def calculate_segment_fare(distance_km, rule):
    if not distance_km or not rule:
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
        raw_fare = base_fare + (steps * add_fare)
    return int(math.ceil((raw_fare + PICKUP_FEE) / 10) * 10)

st.title("🚖 タクシー料金計算（診断デバッグモード）")

start_point = st.text_input("始点（出発地）", value="大阪駅")
end_point = st.text_input("終点（目的地）", value="難波駅")

if st.button("➕ 経由地追加"):
    st.session_state["via_list"].append({"address": "心斎橋駅", "reset_meter": True})
    st.rerun()

for idx, via_item in enumerate(st.session_state["via_list"]):
    c1, c2 = st.columns([2, 1])
    with c1:
        st.session_state["via_list"][idx]["address"] = st.text_input(f"経由地{idx+1}", value=via_item["address"], key=f"via_{idx}")
    with c2:
        st.session_state["via_list"][idx]["reset_meter"] = st.checkbox(f"メーター切り直し", value=via_item["reset_meter"], key=f"chk_{idx}")

if st.button("料金とルートを計算する", type="primary"):
    # 既存の計算結果を強制クリア
    if "calc_result" in st.session_state:
        del st.session_state["calc_result"]

    st.subheader("🔍 リアルタイム判定ログ")
    points = []
    
    # 1. 始点
    s_lat, s_lon = get_coordinates_google(start_point)
    s_area = find_area(s_lat, s_lon)
    points.append({"name": start_point, "lat": s_lat, "lon": s_lon, "reset_after": False, "area": s_area})
    st.write(f"📍 **始点**: {start_point} ➔ エエリア判定: `{s_area['name'] if s_area else '❌ エリア外(None)'}`")

    # 2. 経由地
    for idx, via in enumerate(st.session_state["via_list"]):
        v_lat, v_lon = get_coordinates_google(via["address"])
        v_area = find_area(v_lat, v_lon)
        points.append({"name": via["address"], "lat": v_lat, "lon": v_lon, "reset_after": via["reset_meter"], "area": v_area})
        st.write(f"📍 **経由地{idx+1}**: {via['address']} (メーター切り直し: {via['reset_meter']}) ➔ エリア判定: `{v_area['name'] if v_area else '❌ エリア外(None)'}`")

    # 3. 終点
    e_lat, e_lon = get_coordinates_google(end_point)
    e_area = find_area(e_lat, e_lon)
    points.append({"name": end_point, "lat": e_lat, "lon": e_lon, "reset_after": False, "area": e_area})
    st.write(f"📍 **終点**: {end_point} ➔ エリア判定: `{e_area['name'] if e_area else '❌ エリア外(None)'}`")

    # 全体判定チェック
    global_fallback = next((p["area"] for p in points if p["area"] is not None), None)
    
    st.markdown("---")
    if points[0]["area"] is None and points[-1]["area"] is None:
        st.error("⛔ 判定結果: 始点および終点の両方が営業エリア外のため、エラー停止します。")
    else:
        st.success(f"✅ 判定結果: エリア内として計算を開始します。（全体適用エリア: {global_fallback['name']}）")
        
        # 区間分割
        meter_segments = []
        curr = [points[0]]
        for i in range(1, len(points)):
            curr.append(points[i])
            if points[i-1]["reset_after"] or i == len(points)-1:
                meter_segments.append(curr)
                if i < len(points)-1:
                    curr = [points[i]]
        
        total_fare = 0
        for s_idx, seg in enumerate(meter_segments):
            p_start, p_end = seg[0], seg[-1]
            rule = p_start["area"] or p_end["area"] or global_fallback
            dist = get_google_route(p_start["lat"], p_start["lon"], p_end["lat"], p_end["lon"])
            fare = calculate_segment_fare(dist, rule)
            total_fare += fare
            st.info(f"🚗 **区間 {s_idx+1}** ({p_start['name']} ➔ {p_end['name']}): {dist:.2f}km / {fare:,}円 (適用ルール: {rule['name']})")
            
        st.metric("運賃合計", f"{total_fare:,} 円")