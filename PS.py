import streamlit as st
import requests
import json
import math
import glob
import os
import folium
from streamlit_folium import st_folium
from shapely.geometry import shape, Point

# ---------------------------------------------------------
# パスワード認証処理
# ---------------------------------------------------------
def check_password():
    """SecretsにPASSWORDが設定されている場合のみ認証を行う"""
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

# ---------------------------------------------------------
# Session State 初期化
# ---------------------------------------------------------
if "start_point_val" not in st.session_state:
    st.session_state["start_point_val"] = ""
if "end_point_val" not in st.session_state:
    st.session_state["end_point_val"] = ""

# 経由地の動的リスト (最大3件)
# 各要素: {"address": "", "reset_meter": True, "coords": None}
if "via_list" not in st.session_state:
    st.session_state["via_list"] = []

if "start_coords" not in st.session_state:
    st.session_state["start_coords"] = None
if "end_coords" not in st.session_state:
    st.session_state["end_coords"] = None

if "last_processed_click" not in st.session_state:
    st.session_state["last_processed_click"] = None

# ---------------------------------------------------------
# GeoJSON読み込み & エリア判定
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

        import polyline
        path_coords = polyline.decode(route["overview_polyline"]["points"])

        return {
            "distance_km": distance_km,
            "path_coords": path_coords
        }
    except Exception as e:
        st.error(f"Google Directions API 通信エラー: {e}")
        return None

# ---------------------------------------------------------
# HERE API 関数（高速料金取得）
# ---------------------------------------------------------
def get_here_toll_fee(origin_lat, origin_lon, dest_lat, dest_lon, avoid_highways=False, path_coords=None):
    """
    HERE Routing API v8 で高速料金を取得
    path_coords (Googleから取得したルート座標群) があれば、中間地点を via に追加してルートを固定する
    """
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

    # Googleのルート座標から中間地点（ウェイポイント）抽出してHEREに強制通過させる
    # これによりGoogleとHEREの通過高速道路・ICのズレを防止する
    if path_coords and len(path_coords) > 10:
        # 1/4 と 3/4 の地点を midway (via) として設定
        idx1 = len(path_coords) // 4
        idx2 = (len(path_coords) * 3) // 4
        via1 = f"{path_coords[idx1][0]},{path_coords[idx1][1]}"
        via2 = f"{path_coords[idx2][0]},{path_coords[idx2][1]}"
        params["via"] = [via1, via2]

    try:
        res = requests.get(url, params=params).json()
        if "routes" not in res or len(res["routes"]) == 0:
            return 0

        total_toll_cost = 0
        sections = res["routes"][0].get("sections", [])
        
        for section in sections:
            if "tolls" in section:
                for toll in section["tolls"]:
                    # fares (料金リスト) から円表記(JPY) の基本料金を取得
                    for fare in toll.get("fares", []):
                        price_val = fare.get("price", {}).get("value", 0)
                        if price_val > 0:
                            total_toll_cost += int(price_val)
                            
        return total_toll_cost
    except Exception as e:
        print(f"HERE Toll API Error: {e}")
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
        center = [34.7024, 135.4959]  # 大阪駅周辺
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
st.title("🚖 タクシー料金計算アプリ")

col1, col2 = st.columns(2)
with col1:
    start_point = st.text_input("始点（出発地）", value=st.session_state["start_point_val"])
    st.session_state["start_point_val"] = start_point

with col2:
    end_point = st.text_input("終点（目的地）", value=st.session_state["end_point_val"])
    st.session_state["end_point_val"] = end_point

# ---------------------------------------------------------
# 経由地フォームエリア（最大3件）
# ---------------------------------------------------------
st.markdown("### 経由地設定（最大3件）")

# 経由地の追加・削除ボタン
col_b1, col_b2, _ = st.columns([1, 1, 2])
with col_b1:
    if len(st.session_state["via_list"]) < 3:
        if st.button("➕ 経由地を追加する"):
            st.session_state["via_list"].append({"address": "", "reset_meter": True, "coords": None})
            st.rerun()
with col_b2:
    if len(st.session_state["via_list"]) > 0:
        if st.button("🗑️ 経由地を減らす"):
            st.session_state["via_list"].pop()
            st.rerun()

# 各経由地の入力フォーム表示
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

# ---------------------------------------------------------
# 地図からの地点設定エリア
# ---------------------------------------------------------
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
if st.button("料金とルートを計算する", type="primary"):
    if GOOGLE_MAPS_API_KEY == "YOUR_GOOGLE_API_KEY" or not GOOGLE_MAPS_API_KEY:
        st.error("Secrets またはコード内に Google Maps API Key を設定してください。")
    elif not start_point or not end_point:
        st.warning("始点と終点を入力してください。")
    elif any(not v["address"] for v in st.session_state["via_list"]):
        st.warning("入力されていない経由地があります。住所を入力するか「経由地を減らす」を押してください。")
    else:
        with st.spinner("Google Routes と HERE 高速料金を計算中..."):
            avoid_highways = not use_highway
            
            # 地点リストの作成（始点 ➔ 経由地1 ➔ 経由地2... ➔ 終点）
            points = []
            
            # 始点
            s_lat, s_lon = get_coordinates_google(start_point)
            if s_lat is None:
                st.error("始点の位置情報が取得できませんでした。")
                st.stop()
            st.session_state["start_coords"] = (s_lat, s_lon)
            points.append({"name": start_point, "lat": s_lat, "lon": s_lon, "reset_after": True, "type": "start"})

            # 経由地
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

            # 終点
            e_lat, e_lon = get_coordinates_google(end_point)
            if e_lat is None:
                st.error("終点の位置情報が取得できませんでした。")
                st.stop()
            st.session_state["end_coords"] = (e_lat, e_lon)
            points.append({"name": end_point, "lat": e_lat, "lon": e_lon, "reset_after": False, "type": "end"})

            # エリア情報の取得
            for pt in points:
                pt["area"] = find_area(pt["lat"], pt["lon"])

            default_rule = {"name": "標準運賃エリア", "base_fare": 500, "base_distance_m": 1000, "add_fare": 100, "add_distance_m": 250}

            # メーター区間ごとにルート・距離・料金を統合計算
            # reset_after=True ごとに「1つのメーター区間」としてまとめる
            meter_segments = []
            current_segment_pts = [points[0]]

            for i in range(1, len(points)):
                current_segment_pts.append(points[i])
                # 前の地点でメーターを切る設定、または最終地点に達した場合に区間確定
                if points[i - 1]["reset_after"] or i == len(points) - 1:
                    meter_segments.append(current_segment_pts)
                    current_segment_pts = [points[i]]

            all_path_coords = []
            total_distance = 0.0
            api_toll_fee = 0
            taxi_fare = 0
            error_flag = False
            error_message = ""
            info_messages = []
            caption_messages = []

            for seg_idx, seg_pts in enumerate(meter_segments):
                seg_start = seg_pts[0]
                seg_end = seg_pts[-1]
                
                # エリアチェック（始点または終点のどちらかがエリア内である必要あり）
                applied_rule = seg_start["area"] if seg_start["area"] else seg_end["area"]
                if applied_rule is None and ALL_FEATURES:
                    error_flag = True
                    error_message = f"区間 {seg_idx + 1} ({seg_start['name']} ➔ {seg_end['name']}) の発着地が共に営業エリア外です。"
                    break
                
                if applied_rule is None:
                    applied_rule = default_rule

                # 区間内の連続する2点間ごとのルート・走行距離・高速料金を取得
                seg_dist = 0.0
                seg_tolls = 0
                for k in range(len(seg_pts) - 1):
                    p1 = seg_pts[k]
                    p2 = seg_pts[k + 1]

                    route_info = get_google_route(p1["lat"], p1["lon"], p2["lat"], p2["lon"], avoid_highways)
                    if route_info is None:
                        error_flag = True
                        break

                    seg_dist += route_info["distance_km"]
                    all_path_coords.append(route_info["path_coords"])

                    toll = get_here_toll_fee(p1["lat"], p1["lon"], p2["lat"], p2["lon"], avoid_highways)
                    seg_tolls += toll

                if error_flag:
                    break

                seg_fare = calculate_segment_fare(seg_dist, applied_rule, is_night)
                
                total_distance += seg_dist
                api_toll_fee += seg_tolls
                taxi_fare += seg_fare

                if len(meter_segments) > 1:
                    info_messages.append(f"区間{seg_idx + 1} ({seg_start['name']} ➔ {seg_end['name']}) 適用エリア: **{applied_rule['name']}**")
                    caption_messages.append(f"・区間{seg_idx + 1}: {seg_dist:.2f} km / {seg_fare:,} 円 (迎車込)")
                else:
                    info_messages.append(f"適用運賃エリア: **{applied_rule['name']}**")

            # 結果格納
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
# 結果表示
# ---------------------------------------------------------
if "calc_result" in st.session_state:
    res = st.session_state["calc_result"]
    if res.get("error"):
        st.error(f"⛔ {res.get('error_message', '指定地点のルート検索に失敗しました。')}")
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
