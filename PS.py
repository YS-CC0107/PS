import streamlit as st
import requests
import json
import math
import glob
import os
import flexpolyline
import folium
from streamlit_folium import st_folium
from shapely.geometry import shape, Point

# ---------------------------------------------------------
# 設定
# ---------------------------------------------------------
# 💡 ご自身の HERE API Key を設定してください
HERE_API_KEY = st.secrets.get("HERE_API_KEY", "YOUR_HERE_API_KEY")

PICKUP_FEE = 300      # 迎車料金 (1乗車につき固定)
RESERVATION_FEE = 500 # 予約料金 (選択時)

# ---------------------------------------------------------
# Session State 初期化
# ---------------------------------------------------------
if "start_point_val" not in st.session_state:
    st.session_state["start_point_val"] = ""
if "end_point_val" not in st.session_state:
    st.session_state["end_point_val"] = ""
if "via_point_val" not in st.session_state:
    st.session_state["via_point_val"] = ""

# ピンの座標保持用
if "start_coords" not in st.session_state:
    st.session_state["start_coords"] = None
if "end_coords" not in st.session_state:
    st.session_state["end_coords"] = None
if "via_coords" not in st.session_state:
    st.session_state["via_coords"] = None

if "last_processed_click" not in st.session_state:
    st.session_state["last_processed_click"] = None

# ---------------------------------------------------------
# 複数GeoJSONファイル（area_*.geojson）の読み込み & エリア判定
# ---------------------------------------------------------
@st.cache_data
def load_all_area_geojsons():
    all_features = []
    base_dir = os.path.dirname(os.path.abspath(__file__))
    search_pattern = os.path.join(base_dir, "area_*.geojson")
    geojson_files = glob.glob(search_pattern)
    
    if not geojson_files:
        return []

    for file_path in geojson_files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                features = data.get("features", [])
                all_features.extend(features)
        except Exception as e:
            st.error(f"{os.path.basename(file_path)} の読み込みに失敗しました: {e}")

    return all_features

ALL_FEATURES = load_all_area_geojsons()

def find_area(lat, lon):
    if lat is None or lon is None or not ALL_FEATURES:
        return None

    point = Point(lon, lat)

    for feature in ALL_FEATURES:
        polygon = shape(feature["geometry"])
        if polygon.contains(point):
            props = feature["properties"]
            return {
                "name": props.get("name", "名称未設定"),
                "base_fare": int(props.get("base_fare", 500)),
                "base_distance_m": int(props.get("base_distance_m", 1000)),
                "add_fare": int(props.get("add_fare", 100)),
                "add_distance_m": int(props.get("add_distance_m", 250))
            }
    return None

# ---------------------------------------------------------
# HERE API 連携関数
# ---------------------------------------------------------
def get_coordinates_here(address):
    url = "https://geocode.search.hereapi.com/v1/geocode"
    params = {
        "q": address,
        "apiKey": HERE_API_KEY,
        "lang": "ja"
    }
    try:
        response = requests.get(url, params=params)
        data = response.json()
        if "items" in data and len(data["items"]) > 0:
            position = data["items"][0]["position"]
            return position["lat"], position["lng"]
        else:
            st.error(f"住所の検索に失敗しました ({address})")
            return None, None
    except Exception as e:
        st.error(f"HERE Geocoding API 通信エラー: {e}")
        return None, None


def reverse_geocode_here(lat, lon):
    url = "https://revgeocode.search.hereapi.com/v1/revgeocode"
    params = {
        "at": f"{lat},{lon}",
        "apiKey": HERE_API_KEY,
        "lang": "ja"
    }
    try:
        response = requests.get(url, params=params)
        data = response.json()
        if "items" in data and len(data["items"]) > 0:
            return data["items"][0]["address"]["label"]
        return f"{lat:.5f}, {lon:.5f}"
    except Exception:
        return f"{lat:.5f}, {lon:.5f}"


def get_here_route(origin_lat, origin_lon, dest_lat, dest_lon, avoid_highways=False):
    url = "https://router.hereapi.com/v8/routes"
    params = {
        "transportMode": "car",
        "origin": f"{origin_lat},{origin_lon}",
        "destination": f"{dest_lat},{dest_lon}",
        "return": "summary,polyline,tolls",
        "tolls[transponders]": "all",
        "apiKey": HERE_API_KEY,
        "lang": "ja"
    }
    
    if avoid_highways:
        params["avoid[features]"] = "tollRoad"

    try:
        response = requests.get(url, params=params)
        data = response.json()

        if "routes" not in data or len(data["routes"]) == 0:
            st.error("HERE Routing API でルートが見つかりませんでした。")
            return None

        route = data["routes"][0]
        sections = route.get("sections", [])
        
        total_distance_m = 0
        total_toll_cost = 0
        all_coords = []

        for section in sections:
            summary = section.get("summary", {})
            total_distance_m += summary.get("length", 0)

            if "tolls" in section:
                for toll in section["tolls"]:
                    fares = toll.get("fares", [])
                    for fare in fares:
                        price_obj = fare.get("price", {})
                        price_val = price_obj.get("value", 0)
                        if price_val > 0:
                            total_toll_cost += int(price_val)

            if "polyline" in section:
                decoded_tuples = flexpolyline.decode(section["polyline"])
                coords = [(pt[0], pt[1]) for pt in decoded_tuples]
                all_coords.extend(coords)

        distance_km = total_distance_m / 1000.0

        return {
            "distance_km": distance_km,
            "toll_fee": total_toll_cost,
            "path_coords": all_coords
        }
    except Exception as e:
        st.error(f"HERE Routing API 通信エラー: {e}")
        return None

# ---------------------------------------------------------
# タクシー料金計算ロジック
# ---------------------------------------------------------
def calculate_segment_fare(distance_km, rule, is_night):
    if distance_km is None:
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
                
        if current_fare < 5000:
            raw_fare = current_fare
        else:
            raw_fare = 5000 + discounted_extra_fare

    if is_night:
        raw_fare = raw_fare * 1.2

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
        center = [34.7024, 135.4959]  # 大阪駅周辺を中心に表示
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
st.title("🚖 タクシー料金計算アプリ (HERE API 関西対応版)")

col1, col2 = st.columns(2)
with col1:
    start_point = st.text_input("始点（出発地）", value=st.session_state["start_point_val"])
    st.session_state["start_point_val"] = start_point

with col2:
    end_point = st.text_input("終点（目的地）", value=st.session_state["end_point_val"])
    st.session_state["end_point_val"] = end_point

use_via = st.checkbox("経由地を追加する", value=False)
via_point = ""
reset_meter = False

if use_via:
    col_v1, col_v2 = st.columns([2, 1])
    with col_v1:
        via_point = st.text_input("経由地名", value=st.session_state["via_point_val"])
        st.session_state["via_point_val"] = via_point
    with col_v2:
        st.write("")
        st.write("")
        reset_meter = st.checkbox("経由地でメーター切り直し", value=True)

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
        "高速・有料道路料金（円） ※自動取得できない区間の修正・直接入力用",
        min_value=0,
        value=default_toll,
        step=100
    )

# ---------------------------------------------------------
# 地図からの地点設定エリア
# ---------------------------------------------------------
st.markdown("---")
st.markdown("### 🗺️ マップ (クリックして地点を設定)")

click_target_options = ["始点に設定", "終点に設定"]
if use_via:
    click_target_options.insert(1, "経由地に設定")

click_target = st.radio("👇 地図上でクリックした位置の割り当て先を選択してください:", click_target_options, horizontal=True)

# マーカー描画用リスト
current_markers = []
if st.session_state["start_coords"]:
    lat, lon = st.session_state["start_coords"]
    current_markers.append((lat, lon, f"始点: {st.session_state['start_point_val']}", "green"))

if use_via and st.session_state["via_coords"]:
    lat, lon = st.session_state["via_coords"]
    current_markers.append((lat, lon, f"経由地: {st.session_state['via_point_val']}", "orange"))

if st.session_state["end_coords"]:
    lat, lon = st.session_state["end_coords"]
    current_markers.append((lat, lon, f"終点: {st.session_state['end_point_val']}", "red"))

prev_paths = None
if "calc_result" in st.session_state and not st.session_state["calc_result"].get("error"):
    prev_paths = st.session_state["calc_result"].get("all_path_coords")

map_obj = draw_map(current_markers, prev_paths)

map_data = st_folium(
    map_obj,
    width=700,
    height=450,
    key="map_component"
)

# 地図クリック判定
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

        if HERE_API_KEY and HERE_API_KEY != "YOUR_HERE_API_KEY":
            with st.spinner("クリック地点の住所を取得中..."):
                address = reverse_geocode_here(clicked_lat, clicked_lng)
                
                if click_target == "始点に設定":
                    st.session_state["start_point_val"] = address
                    st.session_state["start_coords"] = (clicked_lat, clicked_lng)
                elif click_target == "経由地に設定":
                    st.session_state["via_point_val"] = address
                    st.session_state["via_coords"] = (clicked_lat, clicked_lng)
                elif click_target == "終点に設定":
                    st.session_state["end_point_val"] = address
                    st.session_state["end_coords"] = (clicked_lat, clicked_lng)
                
                st.rerun()

# ---------------------------------------------------------
# 料金計算処理
# ---------------------------------------------------------
st.markdown("---")
if st.button("料金とルートを計算する", type="primary"):
    if HERE_API_KEY == "YOUR_HERE_API_KEY" or not HERE_API_KEY:
        st.error("コード先頭の `HERE_API_KEY` にご自身の HERE API Key を設定してください。")
    elif not start_point or not end_point:
        st.warning("始点と終点を入力してください。")
    elif use_via and not via_point:
        st.warning("経由地名を入力してください。")
    else:
        with st.spinner("HERE API から道路ルートと高速料金を計算中..."):
            avoid_highways = not use_highway
            
            start_lat, start_lon = get_coordinates_here(start_point)
            end_lat, end_lon = get_coordinates_here(end_point)
            via_lat, via_lon = (None, None)
            if use_via:
                via_lat, via_lon = get_coordinates_here(via_point)

            if start_lat is None or end_lat is None or (use_via and via_lat is None):
                st.error("指定された位置情報の取得に失敗しました。")
            else:
                st.session_state["start_coords"] = (start_lat, start_lon)
                st.session_state["end_coords"] = (end_lat, end_lon)
                if use_via:
                    st.session_state["via_coords"] = (via_lat, via_lon)

                start_area = find_area(start_lat, start_lon)
                end_area = find_area(end_lat, end_lon)
                via_area = find_area(via_lat, via_lon) if use_via else None

                all_path_coords = []
                total_distance = 0.0
                api_toll_fee = 0
                taxi_fare = 0
                error_flag = False
                error_message = ""
                info_messages = []
                caption_messages = []

                # デフォルトルール（GeoJSON定義が無い場合）
                default_rule = {"name": "標準運賃エリア", "base_fare": 500, "base_distance_m": 1000, "add_fare": 100, "add_distance_m": 250}

                # -------------------------------------------------
                # 経由地なし
                # -------------------------------------------------
                if not use_via:
                    applied_rule = start_area if start_area else end_area
                    if applied_rule is None and ALL_FEATURES:
                        error_flag = True
                        error_message = "始点・終点のどちらも対象の営業エリア外です。"
                    else:
                        if applied_rule is None:
                            applied_rule = default_rule
                        
                        info_messages.append(f"適用運賃エリア: **{applied_rule['name']}**")
                        route_info = get_here_route(start_lat, start_lon, end_lat, end_lon, avoid_highways)
                        
                        if route_info is None:
                            error_flag = True
                        else:
                            total_distance = route_info["distance_km"]
                            api_toll_fee = route_info["toll_fee"]
                            all_path_coords.append(route_info["path_coords"])
                            taxi_fare = calculate_segment_fare(total_distance, applied_rule, is_night)

                # -------------------------------------------------
                # 経由地あり
                # -------------------------------------------------
                else:
                    route1 = get_here_route(start_lat, start_lon, via_lat, via_lon, avoid_highways)
                    route2 = get_here_route(via_lat, via_lon, end_lat, end_lon, avoid_highways)

                    if route1 is None or route2 is None:
                        error_flag = True
                    else:
                        dist1, dist2 = route1["distance_km"], route2["distance_km"]
                        total_distance = dist1 + dist2
                        api_toll_fee = route1["toll_fee"] + route2["toll_fee"]

                        # A. 経由地でメーター切る場合（2区間独立）
                        if reset_meter:
                            rule1 = start_area if start_area else via_area
                            rule2 = via_area if via_area else end_area

                            # 各区間で発着どちらもエリア外（ALL_FEATURES定義あり時）ならエラー
                            if ALL_FEATURES and (rule1 is None or rule2 is None):
                                error_flag = True
                                error_message = "経由地を含む区間の発着地が営業エリア外です。"
                            else:
                                if rule1 is None: rule1 = default_rule
                                if rule2 is None: rule2 = default_rule

                                fare1 = calculate_segment_fare(dist1, rule1, is_night)
                                fare2 = calculate_segment_fare(dist2, rule2, is_night)
                                taxi_fare = fare1 + fare2

                                all_path_coords.extend([route1["path_coords"], route2["path_coords"]])
                                info_messages.append(f"区間1適用エリア: **{rule1['name']}** / 区間2適用エリア: **{rule2['name']}**")
                                caption_messages.append(f"・区間1 (始点➔経由地): {dist1:.2f} km / {fare1:,} 円 (迎車込)")
                                caption_messages.append(f"・区間2 (経由地➔終点): {dist2:.2f} km / {fare2:,} 円 (迎車込)")

                        # B. メーターを切らない場合（1乗車扱い）
                        else:
                            # 💡 始点または終点のどちらかがエリア内でなければ営業エリア外
                            applied_rule = start_area if start_area else end_area

                            if applied_rule is None and ALL_FEATURES:
                                error_flag = True
                                error_message = "始点・終点のどちらも対象の営業エリア外です（途中の経由地がエリア内でも配車できません）。"
                            else:
                                if applied_rule is None:
                                    applied_rule = default_rule

                                info_messages.append(f"適用運賃エリア: **{applied_rule['name']}**")
                                taxi_fare = calculate_segment_fare(total_distance, applied_rule, is_night)
                                all_path_coords.extend([route1["path_coords"], route2["path_coords"]])

                # -------------------------------------------------
                # 結果格納
                # -------------------------------------------------
                if error_flag:
                    st.session_state["calc_result"] = {
                        "error": True,
                        "error_message": error_message if error_message else "指定地点のルート検索またはエリア判定に失敗しました。"
                    }
                else:
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
                        "info_messages": info_messages,
                        "caption_messages": caption_messages,
                        "all_path_coords": all_path_coords
                    }
                    st.rerun()

# ---------------------------------------------------------
# 計算結果表示エリア
# ---------------------------------------------------------
if "calc_result" in st.session_state:
    res = st.session_state["calc_result"]
    if res.get("error"):
        st.error(f"⛔ {res.get('error_message', '指定地点のルート検索またはエリア判定に失敗しました。')}")
    else:
        st.success("計算が完了しました！")
        
        for msg in res["info_messages"]:
            st.info(msg)
        for msg in res["caption_messages"]:
            st.caption(msg)

        has_toll = res["total_toll_fee"] > 0
        
        if has_toll:
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                st.metric("総実走行距離", f"{res['total_distance']:.2f} km")
            with c2:
                st.metric("タクシー運賃 (迎車込)", f"{res['taxi_fare']:,} 円")
            with c3:
                st.metric("高速料金のみ (ETC)", f"{res['total_toll_fee']:,} 円")
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
            details.append(f"高速料金(ETC): {res['total_toll_fee']:,}円")
        
        st.markdown(f"**【金額内訳】** {' + '.join(details)} ＝ **合計 {res['grand_total']:,}円**")