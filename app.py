"""내 하우스 — 지도 중심 농장 관리 MVP / Python 3.11+

실행
  python -m pip install -r requirements.txt
  streamlit run app.py

배포
  1. GitHub 저장소 루트에 app.py, requirements.txt를 업로드합니다.
  2. Streamlit Community Cloud에서 저장소와 app.py를 선택합니다.
  3. Python 3.11 또는 3.12를 선택하고 배포합니다.
  GitHub Pages는 Python 서버를 실행하지 못합니다.

선택 설정 (Cloud Advanced settings > Secrets 또는 로컬 .streamlit/secrets.toml)
  APP_ACCESS_CODE = "나만의-충분히-긴-가입코드"
  FARM_DB_PATH = "/persistent-volume/farm.sqlite3"
  가입코드를 설정하면 새 계정 생성에 해당 코드가 필요합니다.
  DB 경로는 실제 쓰기 가능한 영구 볼륨이 있을 때만 설정하세요.

중요
  - 로그인별 자료 분리. 비밀번호는 PBKDF2-HMAC-SHA256 + 무작위 salt로 저장.
  - 기본 DB는 이 파일 옆 .farm_data/farm.sqlite3. DB/백업은 GitHub에 올리지 마세요.
  - Community Cloud 로컬 디스크는 영구 보장되지 않습니다. JSON 백업 필수.
    재배포로 계정도 사라지면 새 계정을 만들고 백업을 복원할 수 있습니다.
  - 앱을 연 동안만 날씨를 확인합니다. 백그라운드/휴대폰 푸시 알림은 없습니다.
  - 모형은 대략적인 배치 참고용이며 측량/시공 도면이 아닙니다.
  - 날씨는 하우스 외부 예보입니다. 내부 환경 측정이나 관수 자동 제어가 아닙니다.
  - Open-Meteo 무료 API는 비상업적 시제품용. 상업 운영 전 요금/이용 조건을
    확인하고 적절한 계약 및 API 인증을 추가하세요: https://open-meteo.com/en/terms
  - 지도: OpenStreetMap (https://www.openstreetmap.org/copyright).
    외부 지도/날씨 서비스에 조회 위치가 전달됩니다.
  - 소규모 시제품입니다. 공개 서비스에는 영구 DB, 가입/로그인 속도 제한,
    계정 복구/삭제, 운영 모니터링, 개인정보 안내를 추가해야 합니다.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import html
import json
import math
import secrets
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import folium
import requests
import streamlit as st
from streamlit_folium import st_folium

KST = ZoneInfo("Asia/Seoul")
KINDS = ["비닐하우스", "노지 밭"]
COLORS = {"비닐하우스": "#19705B", "노지 밭": "#AB6927"}
MAX_PLOTS, MAX_TASKS, MAX_RECORDS = 100, 5000, 10000


def now() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def today() -> date:
    return datetime.now(KST).date()


def uid() -> str:
    return uuid.uuid4().hex


def setting(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, default))
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return default


def db_connect() -> sqlite3.Connection:
    path = Path(setting("FARM_DB_PATH", str(Path(__file__).resolve().parent / ".farm_data" / "farm.sqlite3")))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db() -> None:
    conn = db_connect()
    try:
        with conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                salt TEXT NOT NULL, password_hash TEXT NOT NULL,
                data TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0)""")
    finally:
        conn.close()


def blank_data() -> dict:
    return {"schema_version": 1, "farm_name": "내 농장", "center": [35.8242, 127.1480],
            "plots": [], "tasks": [], "records": [],
            "thresholds": {"rain": 60, "wind": 10, "heat": 33, "cold": 0}}


def password_hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 310000).hex()


def create_account(username: str, password: str) -> None:
    salt = secrets.token_hex(16)
    conn = db_connect()
    try:
        with conn:
            conn.execute("INSERT INTO users(username,salt,password_hash,data) VALUES(?,?,?,?)",
                         (username, salt, password_hash(password, salt), json.dumps(blank_data(), ensure_ascii=False)))
    finally:
        conn.close()


def authenticate(username: str, password: str) -> int | None:
    conn = db_connect()
    try:
        row = conn.execute("SELECT id,salt,password_hash FROM users WHERE username=?", (username,)).fetchone()
    finally:
        conn.close()
    # 존재하지 않는 계정도 같은 해시 계산을 수행합니다.
    salt = row["salt"] if row else "0" * 32
    digest = password_hash(password, salt)
    if row and hmac.compare_digest(digest, row["password_hash"]):
        return int(row["id"])
    return None


def load_data(user_id: int) -> tuple[dict, int]:
    conn = db_connect()
    try:
        row = conn.execute("SELECT data,revision FROM users WHERE id=?", (user_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        st.session_state.clear()
        st.rerun()
    return json.loads(row["data"]), int(row["revision"])


def save_data(user_id: int, revision: int, data: dict) -> bool:
    conn = db_connect()
    try:
        with conn:
            result = conn.execute("UPDATE users SET data=?,revision=revision+1 WHERE id=? AND revision=?",
                                  (json.dumps(data, ensure_ascii=False, allow_nan=False), user_id, revision))
            return result.rowcount == 1
    finally:
        conn.close()


def commit(data: dict, revision: int, message: str) -> None:
    valid = validate_data(data)
    if save_data(st.session_state.user_id, revision, valid):
        st.session_state.flash = message
    else:
        st.session_state.flash = "다른 창에서 자료가 변경되어 저장하지 못했어요. 최신 화면에서 다시 시도해주세요."
    st.rerun()


def number(value, low: float, high: float) -> float:
    if isinstance(value, bool):
        raise ValueError("숫자 항목의 형식이 잘못되었습니다.")
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"숫자는 {low}~{high} 범위여야 합니다.")
    return value


def text(value, limit: int, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError("글자 항목이 비어 있거나 허용 길이를 넘었습니다.")
    return value.strip()


def validate_data(raw: dict) -> dict:
    """외부 JSON을 신뢰하지 않고 허용한 필드만 복사합니다."""
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("이 앱의 버전 1 백업 파일이 아닙니다.")
    result = blank_data()
    result["farm_name"] = text(raw["farm_name"], 50, True)
    center = raw["center"]
    if not isinstance(center, list) or len(center) != 2:
        raise ValueError("농장 위치가 올바르지 않습니다.")
    result["center"] = [number(center[0], -80, 80), number(center[1], -180, 180)]
    th = raw["thresholds"]
    result["thresholds"] = {"rain": number(th["rain"], 0, 100), "wind": number(th["wind"], 1, 60),
                            "heat": number(th["heat"], 15, 55), "cold": number(th["cold"], -30, 15)}
    limits = {"plots": MAX_PLOTS, "tasks": MAX_TASKS, "records": MAX_RECORDS}
    for key, limit in limits.items():
        if not isinstance(raw[key], list) or len(raw[key]) > limit:
            raise ValueError(f"{key} 항목 수가 허용 범위를 넘었습니다.")
    ids = set()
    for p in raw["plots"]:
        pid = text(p["id"], 64, True)
        if pid in ids or p["kind"] not in KINDS:
            raise ValueError("구역 ID 또는 구역 종류가 잘못되었습니다.")
        ids.add(pid)
        zones = p.get("zones", [])
        if not isinstance(zones, list) or len(zones) > 50:
            raise ValueError("내부 구역은 최대 50개입니다.")
        zid = set()
        clean_zones = []
        for z in zones:
            key = text(z["id"], 64, True)
            if key in zid:
                raise ValueError("내부 구역 ID가 중복되었습니다.")
            zid.add(key)
            clean_zones.append({"id": key, "name": text(z["name"], 50, True),
                                "crop": text(z["crop"], 50), "note": text(z["note"], 500)})
        result["plots"].append({"id": pid, "name": text(p["name"], 50, True), "kind": p["kind"],
            "crop": text(p["crop"], 50), "note": text(p["note"], 1000),
            "lat": number(p["lat"], -80, 80), "lon": number(p["lon"], -180, 180),
            "width": number(p["width"], 1, 1000), "length": number(p["length"], 1, 2000),
            "angle": number(p["angle"], 0, 359), "zones": clean_zones})
    for key in ["tasks", "records"]:
        seen = set()
        for item in raw[key]:
            ident = text(item["id"], 64, True)
            if ident in seen or item["plot_id"] not in ids:
                raise ValueError("기록 ID가 중복되었거나 연결된 농장이 없습니다.")
            seen.add(ident)
            clean = {"id": ident, "plot_id": item["plot_id"]}
            if key == "tasks":
                if type(item["done"]) is not bool:
                    raise ValueError("할 일 완료 표시가 잘못되었습니다.")
                clean.update(title=text(item["title"], 200, True),
                             due=date.fromisoformat(item["due"]).isoformat(), done=item["done"])
            else:
                timestamp = datetime.fromisoformat(item["at"])
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=KST)
                clean.update(action=text(item["action"], 100, True), note=text(item["note"], 1000),
                             at=timestamp.astimezone(KST).isoformat(timespec="seconds"))
            result[key].append(clean)
    return result


def rectangle(plot: dict) -> list[list[float]]:
    """중심점, 폭/길이(m), 북쪽 기준 시계방향 각도로 대략적인 경계 계산."""
    theta = math.radians(plot["angle"])
    coords = []
    for x, y in [(-1, -1), (1, -1), (1, 1), (-1, 1)]:
        east = x * plot["width"] / 2
        north = y * plot["length"] / 2
        dx = east * math.cos(theta) + north * math.sin(theta)
        dy = -east * math.sin(theta) + north * math.cos(theta)
        lat = plot["lat"] + dy / 111320
        lon = plot["lon"] + dx / (111320 * math.cos(math.radians(plot["lat"])))
        coords.append([lat, lon])
    return coords


@st.cache_data(ttl=3600, max_entries=200, show_spinner=False)
def search_location(query: str) -> list[dict]:
    response = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                            params={"name": query, "count": 8, "language": "ko", "countryCode": "KR"}, timeout=(5, 15))
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise ValueError("위치 검색 서비스 오류")
    return payload.get("results", [])


@st.cache_data(ttl=900, max_entries=300, show_spinner=False)
def fetch_weather(lat: float, lon: float) -> dict:
    response = requests.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": lat, "longitude": lon, "timezone": "Asia/Seoul", "forecast_days": 3,
        "wind_speed_unit": "ms", "current": "temperature_2m,relative_humidity_2m,wind_speed_10m",
        "hourly": "precipitation_probability,precipitation,wind_gusts_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_gusts_10m_max"
    }, timeout=(5, 20))
    response.raise_for_status()
    payload = response.json()
    if payload.get("error") or not all(k in payload for k in ["current", "hourly", "daily"]):
        raise ValueError("예보 응답 형식이 올바르지 않습니다.")
    payload["fetched_at"] = now()
    return payload


def val(value, unit: str = "") -> str:
    return "자료 없음" if value is None else f"{value:g}{unit}"


def at_index(container: dict, key: str, index: int):
    series = container.get(key, [])
    return series[index] if index < len(series) else None


def alerts_for(weather: dict, plot: dict, thresholds: dict) -> list[str]:
    daily = weather["daily"]
    output = []
    for i, d in enumerate(daily.get("time", [])[:2]):
        rain = at_index(daily, "precipitation_probability_max", i)
        wind = at_index(daily, "wind_gusts_10m_max", i)
        high = at_index(daily, "temperature_2m_max", i)
        low = at_index(daily, "temperature_2m_min", i)
        if rain is not None and rain >= thresholds["rain"]:
            action = "배수로와 피복을 확인하세요. 하우스 물주기는 별도로 판단하세요." if plot["kind"] == "비닐하우스" else "토양 상태와 예정된 물주기·야외 작업을 확인하세요."
            output.append(f"{d} · 강수확률 {rain:g}% — {action}")
        if wind is not None and wind >= thresholds["wind"]:
            output.append(f"{d} · 최대 순간풍속 {wind:g}m/s — 피복·문·지지대 고정 상태를 확인하세요.")
        if high is not None and high >= thresholds["heat"]:
            output.append(f"{d} · 외부 최고기온 {high:g}℃ — 내부 온도와 환기 상태, 작업 중 휴식을 확인하세요.")
        if low is not None and low <= thresholds["cold"]:
            output.append(f"{d} · 외부 최저기온 {low:g}℃ — 재배 작물에 맞는 보온 대책을 확인하세요.")
    return output


def login_screen() -> None:
    st.title("🌱 내 하우스")
    st.write("내 농장을 지도에 놓고, 오늘 할 일을 한눈에 확인하세요.")
    st.info("이메일 가입이 아닙니다. 직접 정한 아이디와 비밀번호로 이용해요. 처음이라면 ‘계정 만들기’를 눌러주세요.")
    st.caption("이미 가입했다면 그때 만든 아이디를 그대로 입력하세요. 이메일 모양으로 아이디를 만들었다면 그 값을 그대로 사용하면 됩니다.")
    login_tab, signup_tab = st.tabs(["로그인 · 기존 회원", "계정 만들기 · 처음 사용자"])
    with login_tab:
        with st.form("login"):
            st.markdown("### 가입할 때 만든 정보로 로그인하세요")
            name = st.text_input("아이디 (가입할 때 정한 이름)", max_chars=40,
                                 placeholder="예: 딸기농장 또는 farmer01", key="login_username",
                                 help="이메일 주소를 새로 입력하는 칸이 아닙니다. 가입할 때 만든 아이디를 대소문자까지 똑같이 입력하세요.")
            password = st.text_input("비밀번호 (가입할 때 정한 비밀번호)", type="password", max_chars=128,
                                     placeholder="가입할 때 만든 비밀번호", key="login_password")
            st.caption("아이디와 비밀번호는 영문 대소문자를 구분합니다. 비밀번호는 오른쪽 눈 모양으로 확인할 수 있어요.")
            submit = st.form_submit_button("로그인하고 내 농장 열기", use_container_width=True, type="primary")
        if submit:
            if not name.strip():
                st.error("아이디가 비어 있어요. 가입할 때 정한 아이디를 입력해주세요.")
            elif not password:
                st.error("비밀번호가 비어 있어요. 가입할 때 정한 비밀번호를 입력해주세요.")
            elif time.time() < st.session_state.get("login_after", 0):
                st.warning("3초 정도 기다린 뒤 로그인 버튼을 다시 눌러주세요.")
            else:
                try:
                    user_id = authenticate(name.strip(), password)
                except (sqlite3.Error, OSError):
                    st.error("계정 저장소에 연결하지 못했어요. 잠시 후 다시 시도하거나 운영자에게 알려주세요.")
                    return
                if user_id:
                    st.session_state.clear()
                    st.session_state.user_id = user_id
                    st.session_state.username = name.strip()
                    st.rerun()
                else:
                    st.session_state.login_after = time.time() + 3
                    st.error("로그인 정보가 맞지 않아요. 가입한 아이디와 비밀번호의 한글·영문, 대소문자를 확인해주세요.")
                    st.info("처음이라면 ‘계정 만들기’ 탭에서 먼저 가입하세요. 전에 사용하던 계정도 서버 자료가 초기화되면 로그인할 수 없어요. 이때는 운영자에게 자료 상태를 확인해주세요.")
    with signup_tab:
        with st.form("signup"):
            st.markdown("### 처음 사용할 아이디와 비밀번호를 정해주세요")
            st.write("이메일 주소나 이메일 인증은 필요하지 않아요. 기억하기 쉬운 나만의 아이디를 만드세요.")
            name = st.text_input("새 아이디 (직접 정한 이름 · 2~40자)", max_chars=40,
                                 placeholder="예: 딸기농장 또는 farmer01", key="signup_username",
                                 help="한글·영문·숫자로 기억하기 쉽게 정하세요. 로그인할 때도 이 아이디를 사용합니다.")
            pw = st.text_input("새 비밀번호 (10~128자)", type="password", max_chars=128,
                               placeholder="10자 이상으로 정해주세요", key="signup_password")
            confirm = st.text_input("새 비밀번호 한 번 더", type="password", max_chars=128,
                                    placeholder="바로 위 비밀번호와 똑같이 입력", key="signup_confirm")
            st.caption("비밀번호 찾기는 아직 지원하지 않으니, 아이디와 비밀번호를 안전한 곳에 보관하세요.")
            gate = setting("APP_ACCESS_CODE")
            code = st.text_input("운영자가 알려준 가입코드", type="password") if gate else ""
            accepted = st.checkbox("농장 자료를 서버에 저장하며, 직접 백업해야 한다는 안내를 확인했습니다.")
            submit = st.form_submit_button("계정 만들고 바로 시작하기", use_container_width=True, type="primary")
        if submit:
            if not (2 <= len(name.strip()) <= 40):
                st.error("새 아이디를 2~40자로 입력해주세요. 예: 딸기농장")
            elif len(pw) < 10 or not pw.strip():
                st.error("새 비밀번호는 공백만 쓰지 말고 10자 이상으로 만들어주세요.")
            elif pw != confirm:
                st.error("비밀번호가 서로 달라요.")
            elif not accepted:
                st.error("자료 저장 및 백업 안내를 확인해주세요.")
            elif gate and not hmac.compare_digest(code.encode("utf-8"), gate.encode("utf-8")):
                st.error("가입코드를 확인해주세요.")
            else:
                try:
                    create_account(name.strip(), pw)
                    user_id = authenticate(name.strip(), pw)
                    if user_id is None:
                        st.warning("계정은 만들어졌지만 자동 로그인하지 못했어요. 로그인 탭에서 다시 시도해주세요.")
                    else:
                        st.session_state.clear()
                        st.session_state.user_id = user_id
                        st.session_state.username = name.strip()
                        st.session_state.flash = f"가입 완료! 아이디는 ‘{name.strip()}’입니다. 다음에도 이 아이디로 로그인하세요."
                        st.rerun()
                except sqlite3.IntegrityError:
                    st.error("이미 사용 중인 아이디예요. 전에 만든 본인 아이디라면 로그인 탭을 이용하고, 새 계정이라면 다른 이름을 정해주세요.")
                except (sqlite3.Error, OSError):
                    st.error("계정 저장 중 문제가 생겼어요. 운영자에게 저장소 상태를 확인해주세요.")
    st.info("시제품 안내: 서버 재시작·재배포로 자료가 사라질 수 있어요. 중요한 자료는 백업 파일을 내려받으세요.")


def add_record(data: dict, plot_id: str, action: str, note: str = "") -> None:
    if len(data["records"]) >= MAX_RECORDS:
        raise ValueError("작업 기록이 최대 개수에 도달했습니다. 백업 후 오래된 기록을 정리해주세요.")
    data["records"].append({"id": uid(), "plot_id": plot_id, "action": action, "note": note, "at": now()})


def plot_editor(data: dict, rev: int) -> None:
    st.subheader("하우스·밭 등록하기")
    st.caption("지도에서 위치를 한 번 누르세요. 아래에 종류와 크기를 넣으면 모형이 생깁니다.")
    target = st.session_state.get("map_pick", {"lat": data["center"][0], "lng": data["center"][1]})
    mode = st.radio("할 작업", ["새로 등록", "기존 구역 수정"], horizontal=True, key="edit_mode")
    old = None
    if mode == "기존 구역 수정":
        if not data["plots"]:
            st.info("먼저 하우스나 밭을 등록해주세요.")
            return
        ident = st.selectbox("수정할 구역", [p["id"] for p in data["plots"]],
                             format_func=lambda x: next(p["name"] for p in data["plots"] if p["id"] == x), key="edit_plot")
        old = next(p for p in data["plots"] if p["id"] == ident)
    move = st.checkbox("방금 지도에서 누른 위치로 이동", key=f"move_{old['id']}") if old else True
    base = old or {"name": "", "kind": KINDS[0], "crop": "", "width": 8.0,
                   "length": 40.0, "angle": 0.0, "note": ""}
    lat = target["lat"] if move else old["lat"]
    lon = target["lng"] if move else old["lon"]
    st.caption(f"저장할 중심 위치: 위도 {lat:.6f} / 경도 {lon:.6f}")
    with st.form(f"plot_form_{old['id'] if old else 'new'}"):
        c1, c2 = st.columns(2)
        name = c1.text_input("이름 *", base["name"], placeholder="예: 딸기 1동", max_chars=50)
        kind = c2.selectbox("종류", KINDS, index=KINDS.index(base["kind"]))
        crop = st.text_input("키우는 작물", base["crop"], placeholder="예: 딸기", max_chars=50)
        c1, c2 = st.columns(2)
        width = c1.number_input("폭 (m)", 1.0, 1000.0, float(base["width"]), step=1.0)
        length = c2.number_input("길이 (m)", 1.0, 2000.0, float(base["length"]), step=1.0)
        angle = st.slider("모형 방향 (0°=남북 방향, 90°=동서 방향)", 0, 359, int(base["angle"]))
        note = st.text_area("메모", base["note"], max_chars=1000)
        submitted = st.form_submit_button("이 구성으로 저장", type="primary", use_container_width=True)
    if submitted:
        if not name.strip():
            st.error("구역 이름을 입력해주세요.")
            return
        if not old and len(data["plots"]) >= MAX_PLOTS:
            st.error("하우스·밭은 최대 100개까지 등록할 수 있어요.")
            return
        p = {"id": old["id"] if old else uid(), "name": name.strip(), "kind": kind,
             "crop": crop.strip(), "lat": lat, "lon": lon, "width": width, "length": length,
             "angle": angle, "note": note, "zones": old["zones"] if old else []}
        if old:
            data["plots"] = [p if item["id"] == old["id"] else item for item in data["plots"]]
        else:
            data["plots"].append(p)
        data["center"] = [lat, lon]
        st.session_state.focus_plot = p["id"]
        commit(data, rev, "하우스 구성을 저장했어요.")


def task_list(data: dict, rev: int, plot_id: str | None = None) -> None:
    names = {p["id"]: p["name"] for p in data["plots"]}
    tasks = [t for t in data["tasks"] if plot_id is None or t["plot_id"] == plot_id]
    show_done = st.checkbox("완료한 일도 보기", key=f"show_done_{plot_id}")
    filtered = sorted([t for t in tasks if show_done or not t["done"]], key=lambda t: (t["done"], t["due"]))
    if not filtered:
        st.info("등록된 미완료 작업이 없어요. 아래에서 할 일을 추가할 수 있어요.")
    # 긴 목록은 페이지 단위로 표시합니다.
    pages = max(1, math.ceil(len(filtered) / 30))
    page = st.number_input("작업 목록 페이지", 1, pages, 1, key=f"task_page_{plot_id}") if pages > 1 else 1
    for task in filtered[(page - 1) * 30:page * 30]:
        with st.container(border=True):
            overdue = not task["done"] and task["due"] < today().isoformat()
            st.write(f"{'✅' if task['done'] else '🔴' if overdue else '⬜'} {task['title']}")
            st.caption(f"{names[task['plot_id']]} · {task['due']} {'· 예정일 지남' if overdue else ''}")
            if st.button("완료 취소" if task["done"] else "했어요 ✓", key=f"task_{task['id']}", use_container_width=True):
                task["done"] = not task["done"]
                add_record(data, task["plot_id"], "할 일 완료" if task["done"] else "완료 취소", task["title"])
                commit(data, rev, "작업 상태를 저장했어요.")
    if not data["plots"]:
        return
    with st.container(border=True):
        st.markdown("#### ＋ 새로운 할 일 추가")
        with st.form(f"new_task_{plot_id}", clear_on_submit=True):
            selected = plot_id or st.selectbox("어느 구역인가요?", list(names), format_func=names.get)
            title = st.text_input("할 일", placeholder="예: A행 물주기 / 고정 끈 확인", max_chars=200)
            due = st.date_input("언제 할까요?", today())
            submit = st.form_submit_button("할 일 저장", use_container_width=True)
        if submit:
            if not title.strip():
                st.error("할 일을 입력해주세요.")
            elif len(data["tasks"]) >= MAX_TASKS:
                st.error("할 일이 최대 개수에 도달했습니다. 백업 후 완료한 일을 정리해주세요.")
            else:
                data["tasks"].append({"id": uid(), "plot_id": selected, "title": title.strip(), "due": due.isoformat(), "done": False})
                commit(data, rev, "할 일을 추가했어요.")


def plot_details(data: dict, rev: int) -> None:
    if not data["plots"]:
        return
    st.divider()
    st.subheader("우리 농장 살펴보기")
    names = {p["id"]: p["name"] for p in data["plots"]}
    if st.session_state.get("focus_plot") not in names:
        st.session_state.focus_plot = next(iter(names))
    ident = st.selectbox("어느 하우스·밭을 볼까요?", list(names), format_func=names.get, key="focus_plot")
    p = next(p for p in data["plots"] if p["id"] == ident)
    st.write(f"{p['kind']} · 작물: {p['crop'] or '미등록'} · {p['width']:g}m × {p['length']:g}m")
    if p["note"]:
        st.text(p["note"])
    st.markdown("#### 방금 한 일, 한 번만 누르세요")
    cols = st.columns(3)
    for col, action in zip(cols, ["물 줬어요", "비료 줬어요", "수확했어요"]):
        if col.button(action, key=f"quick_{ident}_{action}", use_container_width=True):
            add_record(data, ident, action)
            commit(data, rev, f"‘{action}’를 기록했어요.")
    with st.expander("직접 작업 기록 남기기"):
        with st.form(f"record_{ident}", clear_on_submit=True):
            action = st.text_input("작업 이름", max_chars=100, placeholder="예: A행 영양제 주기")
            note = st.text_area("상세 내용 (선택)", max_chars=1000)
            submit = st.form_submit_button("기록 저장", use_container_width=True)
        if submit:
            if action.strip():
                add_record(data, ident, action.strip(), note)
                commit(data, rev, "작업을 기록했어요.")
            else:
                st.error("작업 이름을 입력해주세요.")
    with st.expander("하우스 안 구역 · A행 / B열 관리"):
        st.caption("내부 구역은 이름과 작물로 관리합니다. 내부 3D 배치·자동 설계는 제공하지 않습니다.")
        for z in p["zones"]:
            with st.container(border=True):
                st.text(f"{z['name']} · {z['crop'] or '작물 미등록'}")
                if z["note"]:
                    st.text(z["note"])
        choices = ["new"] + [z["id"] for z in p["zones"]]
        selected = st.selectbox("추가하거나 수정할 내부 구역", choices,
            format_func=lambda x: "＋ 새 구역" if x == "new" else next(z["name"] for z in p["zones"] if z["id"] == x), key=f"zone_choice_{ident}")
        zone = next((z for z in p["zones"] if z["id"] == selected), {"name": "", "crop": "", "note": ""})
        with st.form(f"zone_{ident}_{selected}"):
            name = st.text_input("구역 이름", zone["name"], placeholder="예: A행", max_chars=50)
            crop = st.text_input("구역 작물", zone["crop"], max_chars=50)
            note = st.text_input("구역 메모", zone["note"], max_chars=500)
            submit = st.form_submit_button("내부 구역 저장")
        if submit:
            if not name.strip():
                st.error("내부 구역 이름을 입력해주세요.")
            elif selected == "new" and len(p["zones"]) >= 50:
                st.error("내부 구역은 최대 50개입니다.")
            else:
                clean = {"id": uid() if selected == "new" else selected, "name": name.strip(), "crop": crop, "note": note}
                p["zones"] = p["zones"] + [clean] if selected == "new" else [clean if z["id"] == selected else z for z in p["zones"]]
                commit(data, rev, "내부 구역을 저장했어요.")
        if selected != "new" and st.checkbox("선택한 내부 구역을 삭제할게요", key=f"z_del_check_{ident}_{selected}"):
            if st.button("내부 구역 삭제", key=f"z_del_{ident}_{selected}"):
                p["zones"] = [z for z in p["zones"] if z["id"] != selected]
                commit(data, rev, "내부 구역을 삭제했어요.")
    with st.expander("이 구역의 할 일"):
        task_list(data, rev, ident)
    with st.expander("최근 작업 기록", expanded=True):
        records = sorted([r for r in data["records"] if r["plot_id"] == ident], key=lambda r: r["at"], reverse=True)
        if not records:
            st.caption("아직 기록이 없어요.")
        for r in records[:30]:
            st.text(f"{r['at'][:16].replace('T', ' ')} · {r['action']}" + (f"\n{r['note']}" if r["note"] else ""))
        if records:
            record_id = st.selectbox("잘못 남긴 기록 선택", [r["id"] for r in records[:30]],
                format_func=lambda x: next(f"{r['at'][:16]} · {r['action']}" for r in records if r["id"] == x), key=f"record_delete_{ident}")
            if st.checkbox("선택한 기록 삭제 확인", key=f"record_confirm_{ident}"):
                if st.button("선택 기록 삭제", key=f"delete_record_{ident}"):
                    data["records"] = [r for r in data["records"] if r["id"] != record_id]
                    commit(data, rev, "선택한 기록을 삭제했어요. 할 일 완료 상태는 바뀌지 않아요.")
    with st.expander("이 하우스·밭 삭제"):
        st.warning("이 구역과 연결된 할 일·작업 기록도 함께 삭제됩니다. 먼저 백업해주세요.")
        confirmed = st.checkbox("연결된 자료까지 삭제하는 데 동의합니다.", key=f"delete_confirm_{ident}")
        if st.button("이 구역 삭제", key=f"delete_plot_{ident}", disabled=not confirmed):
            data["plots"] = [p for p in data["plots"] if p["id"] != ident]
            data["tasks"] = [t for t in data["tasks"] if t["plot_id"] != ident]
            data["records"] = [r for r in data["records"] if r["plot_id"] != ident]
            st.session_state.pop("focus_plot", None)
            commit(data, rev, "구역을 삭제했어요.")


def farm_page(data: dict, rev: int) -> None:
    st.subheader("🗺️ 내 농장 지도")
    st.caption("① 농장 위치 찾기 → ② 지도에서 위치 누르기 → ③ 아래에서 하우스·밭 등록")
    with st.expander("농장 위치 찾기 / 지도 이동", expanded=not data["plots"]):
        with st.form("search"):
            query = st.text_input("시·군·읍·면 이름", placeholder="예: 논산 / Nonsan")
            search = st.form_submit_button("위치 찾기")
        if search:
            if len(query.strip()) < 2:
                st.warning("두 글자 이상 입력해주세요.")
            else:
                try:
                    with st.spinner("지역을 찾고 있어요…"):
                        st.session_state.location_results = search_location(query.strip())
                    if not st.session_state.location_results:
                        st.info("검색 결과가 없어요. 더 큰 지역명이나 영문명으로 검색하거나 좌표를 입력해주세요.")
                except (requests.RequestException, ValueError, KeyError):
                    st.error("위치 검색에 연결하지 못했어요. 아래 좌표 입력이나 지도 이동을 이용해주세요.")
        results = st.session_state.get("location_results", [])
        if results:
            idx = st.selectbox("검색한 지역", range(len(results)), format_func=lambda i: " · ".join(str(results[i].get(k, "")) for k in ["name", "admin1", "admin2"]))
            if st.button("이 지역으로 지도 이동"):
                data["center"] = [results[idx]["latitude"], results[idx]["longitude"]]
                st.session_state.pop("map_pick", None)
                commit(data, rev, "지도 위치를 옮겼어요. 확대해서 농장 위치를 눌러주세요.")
        with st.form("coordinates"):
            c1, c2 = st.columns(2)
            lat = c1.number_input("직접 입력: 위도", -80.0, 80.0, float(data["center"][0]), format="%.6f")
            lon = c2.number_input("직접 입력: 경도", -180.0, 180.0, float(data["center"][1]), format="%.6f")
            go = st.form_submit_button("이 좌표로 이동")
        if go:
            data["center"] = [lat, lon]
            st.session_state.pop("map_pick", None)
            commit(data, rev, "지도 위치를 옮겼어요.")
        st.caption("검색은 지역명 중심이며 정확한 번지 검색은 지원하지 않습니다. 현재 첫 위치는 예시 중심점입니다.")
    m = folium.Map(location=data["center"], zoom_start=18, max_zoom=19, tiles="OpenStreetMap", control_scale=True)
    for p in data["plots"]:
        pending = sum(1 for t in data["tasks"] if t["plot_id"] == p["id"] and not t["done"] and t["due"] <= today().isoformat())
        safe_name = html.escape(p["name"])
        safe_crop = html.escape(p["crop"] or "작물 미등록")
        # HTML/템플릿 리터럴 내부의 사용자 입력을 실행하지 않도록 escape합니다.
        def safe_map(s: str) -> str:
            return s.replace("`", "&#96;").replace("$", "&#36;").replace("\\", "&#92;")
        label = safe_map(safe_name)
        popup = f"<b>{label}</b><br>{safe_map(safe_crop)}<br>오늘까지 미완료: {pending}건<br>상세 정보는 지도 아래에서 선택하세요."
        color = COLORS[p["kind"]]
        folium.Polygon(rectangle(p), color=color, weight=3, fill=True, fill_color=color,
                       fill_opacity=0.3, tooltip=label, popup=folium.Popup(popup, max_width=270)).add_to(m)
        folium.Marker([p["lat"], p["lon"]], tooltip=label,
            popup=folium.Popup(popup, max_width=270),
            icon=folium.Icon(color="green" if p["kind"] == "비닐하우스" else "orange", icon="home" if p["kind"] == "비닐하우스" else "leaf")).add_to(m)
    result = st_folium(m, height=430, use_container_width=True,
                       key=f"farm_map_{st.session_state.user_id}_{rev}", returned_objects=["last_clicked"])
    if result and result.get("last_clicked"):
        point = result["last_clicked"]
        if -80 <= point["lat"] <= 80 and -180 <= point["lng"] <= 180:
            st.session_state.map_pick = point
    st.caption("초록 = 비닐하우스 · 주황 = 노지 밭. 지도와 모형은 실제 측량·시공 도면이 아닙니다.")
    with st.expander("＋ 하우스·밭 등록 / 크기·위치 수정", expanded=not data["plots"]):
        plot_editor(data, rev)
    plot_details(data, rev)


def weather_page(data: dict) -> None:
    st.subheader("🌦️ 우리 농장 날씨")
    st.info("앱을 열어 확인하는 알림입니다. 앱이 닫혀 있을 때의 휴대폰 푸시·문자 알림은 지원하지 않습니다.")
    if not data["plots"]:
        st.warning("지도에서 하우스나 밭을 먼저 등록해주세요.")
        return
    names = {p["id"]: p["name"] for p in data["plots"]}
    ident = st.selectbox("날씨를 확인할 구역", list(names), format_func=names.get)
    p = next(p for p in data["plots"] if p["id"] == ident)
    st.caption(f"조회 위치: {p['lat']:.5f}, {p['lon']:.5f} · 한국시간 기준 · 최대 15분 캐시")
    lat, lon = round(p["lat"], 5), round(p["lon"], 5)
    if st.button("예보 새로 확인", use_container_width=True):
        fetch_weather.clear(lat, lon)
    try:
        with st.spinner("농장 위치의 예보를 확인하고 있어요…"):
            weather = fetch_weather(lat, lon)
    except (requests.RequestException, ValueError, KeyError):
        st.error("날씨 서버에 연결하지 못했어요. 잠시 후 다시 확인해주세요. 농장 기록은 계속 사용할 수 있어요.")
        return
    current = weather["current"]
    c1, c2, c3 = st.columns(3)
    c1.metric("외부 기온", val(current.get("temperature_2m"), "℃"))
    c2.metric("외부 습도", val(current.get("relative_humidity_2m"), "%"))
    c3.metric("외부 풍속", val(current.get("wind_speed_10m"), "m/s"))
    st.caption(f"현재 값의 예보 기준시각: {current.get('time', '알 수 없음')} · 조회: {weather['fetched_at'][:16].replace('T', ' ')}")
    st.markdown("#### 오늘·내일 확인할 일")
    alerts = alerts_for(weather, p, data["thresholds"])
    for alert in alerts:
        st.warning(alert)
    if not alerts:
        st.success("가져온 예보 중 설정한 알림 기준을 넘는 항목은 없어요. 안전을 보장하는 의미는 아닙니다.")
    st.caption("알림 기준은 사용자가 정한 참고값이며 기상특보 기준이 아닙니다. 백업·설정에서 변경할 수 있어요.")
    hourly = weather["hourly"]
    upcoming = []
    current_time = datetime.now(KST)
    for i, hour in enumerate(hourly.get("time", [])):
        stamp = datetime.fromisoformat(hour).replace(tzinfo=KST)
        if current_time <= stamp <= current_time + timedelta(hours=24):
            prob = at_index(hourly, "precipitation_probability", i)
            rain = at_index(hourly, "precipitation", i)
            if (prob is not None and prob >= data["thresholds"]["rain"]) or (rain is not None and rain >= 0.1):
                upcoming.append(f"{stamp:%m/%d %H:%M} · 강수확률 {val(prob, '%')} · 예상 강수량 {val(rain, 'mm')}")
    with st.expander("앞으로 24시간 · 비 예보 시간대", expanded=bool(upcoming)):
        if upcoming:
            for line in upcoming:
                st.text(line)
        else:
            st.write("설정한 강수확률 기준 또는 0.1mm 이상 강수 예보에 해당하는 시간대가 없어요.")
        st.caption("자료가 없는 시간은 판단할 수 없습니다. 실제 비의 시작·종료 시각은 달라질 수 있어요.")
    st.markdown("#### 3일 예보")
    daily = weather["daily"]
    rows = []
    for i, d in enumerate(daily.get("time", [])):
        rows.append({"날짜": d, "최저": val(at_index(daily, "temperature_2m_min", i), "℃"),
                     "최고": val(at_index(daily, "temperature_2m_max", i), "℃"),
                     "강수확률": val(at_index(daily, "precipitation_probability_max", i), "%"),
                     "강수량": val(at_index(daily, "precipitation_sum", i), "mm"),
                     "최대 순간풍속": val(at_index(daily, "wind_gusts_10m_max", i), "m/s")})
    st.dataframe(rows, hide_index=True, use_container_width=True)
    st.warning("외부 예보는 하우스 내부 측정값이 아닙니다. 비가 와도 하우스 관수를 자동으로 취소하지 마세요. 실제 토양·작물 상태를 확인하세요.")
    st.markdown("예보 출처: [Open-Meteo](https://open-meteo.com/) · 긴급 기상정보는 [기상청 날씨누리](https://www.weather.go.kr/)에서 확인하세요.")


def backup_page(data: dict, rev: int) -> None:
    st.subheader("📦 백업·설정")
    st.warning("이 서버의 로컬 저장소는 영구 보장되지 않습니다. 중요한 기록은 정기적으로 내려받으세요.")
    st.download_button("농장 자료 백업 내려받기", json.dumps(data, ensure_ascii=False, indent=2),
                       file_name=f"my_farm_{today().isoformat()}.json", mime="application/json", use_container_width=True, type="primary")
    st.caption("백업에는 농장 위치와 작업 기록이 포함됩니다. 비밀번호·계정 정보는 포함되지 않습니다. 공개 저장소에 올리지 마세요.")
    with st.expander("백업 파일 복원"):
        uploaded = st.file_uploader("이 앱에서 받은 JSON 백업", type=["json"])
        st.warning("복원하면 현재 계정의 농장 자료 전체를 바꿉니다. 복원 전에 현재 자료도 백업하세요.")
        replace = st.checkbox("현재 자료를 백업 파일 내용으로 바꾸겠습니다.")
        if st.button("백업 복원", disabled=not uploaded or not replace):
            try:
                if uploaded.size > 10 * 1024 * 1024:
                    raise ValueError("백업 파일은 10MB 이하여야 합니다.")
                restored = validate_data(json.loads(uploaded.getvalue().decode("utf-8-sig")))
                st.session_state.pop("focus_plot", None)
                st.session_state.pop("map_pick", None)
                commit(restored, rev, "백업 자료를 복원했어요.")
            except (ValueError, KeyError, TypeError, UnicodeError, OverflowError, RecursionError) as exc:
                st.error(f"백업 형식을 확인해주세요. ({type(exc).__name__})")
    with st.form("preferences"):
        st.markdown("#### 농장 이름과 날씨 알림 기준")
        name = st.text_input("농장 이름", data["farm_name"], max_chars=50)
        t = data["thresholds"]
        rain = st.slider("비 확인: 강수확률 이상 (%)", 0, 100, int(t["rain"]))
        wind = st.number_input("강풍 확인: 최대 순간풍속 이상 (m/s)", 1.0, 60.0, float(t["wind"]))
        heat = st.number_input("더위 확인: 외부 최고기온 이상 (℃)", 15.0, 55.0, float(t["heat"]))
        cold = st.number_input("추위 확인: 외부 최저기온 이하 (℃)", -30.0, 15.0, float(t["cold"]))
        st.caption("기본값은 예시입니다. 작물·생육 단계·시설에 맞게 직접 정하세요. 공식 기상특보가 아닙니다.")
        submit = st.form_submit_button("설정 저장", use_container_width=True)
    if submit:
        if not name.strip():
            st.error("농장 이름을 입력해주세요.")
        else:
            data["farm_name"] = name.strip()
            data["thresholds"] = {"rain": rain, "wind": wind, "heat": heat, "cold": cold}
            commit(data, rev, "설정을 저장했어요.")
    with st.expander("오래된 자료 정리"):
        st.caption("정리 전 백업을 내려받으세요. 선택한 날짜 이전의 작업 기록과 완료한 할 일만 삭제합니다. 미완료 할 일은 남깁니다.")
        cutoff = st.date_input("이 날짜 이전 자료 정리", today() - timedelta(days=365))
        confirm = st.checkbox("선택한 오래된 자료를 삭제할게요.")
        if st.button("오래된 자료 삭제", disabled=not confirm):
            data["records"] = [r for r in data["records"] if r["at"][:10] >= cutoff.isoformat()]
            data["tasks"] = [t for t in data["tasks"] if not t["done"] or t["due"] >= cutoff.isoformat()]
            commit(data, rev, "오래된 자료를 정리했어요.")
    st.markdown("#### 서비스 안내")
    st.write("가족과 서로 다른 계정을 쓰면 자료도 분리됩니다. 가족 공동 편집·사진 첨부·센서 연결·푸시 알림은 이 버전에 포함되지 않습니다.")
    st.markdown("지도: [© OpenStreetMap contributors](https://www.openstreetmap.org/copyright) · 예보/지역검색: [Open-Meteo](https://open-meteo.com/)")
    st.caption("외부 서비스에 조회 좌표 또는 검색어가 전달됩니다. 무료 API의 상업적 사용 조건과 지도 이용 정책을 확인한 뒤 운영하세요.")


def main() -> None:
    st.set_page_config(page_title="내 하우스 · 내 농장 관리", page_icon="🌱", layout="wide", initial_sidebar_state="collapsed")
    st.markdown("""<style>
    :root {color-scheme: light;}
    .stApp {background:#F6F8F3; color:#1C3028;}
    .block-container {max-width:1120px; padding-top:2rem; padding-bottom:4rem;}
    h1,h2,h3 {letter-spacing:-.04em;}
    div.stButton > button, div.stFormSubmitButton > button,
    div.stDownloadButton > button {min-height:52px; font-size:18px; border-radius:12px;}
    div.stButton > button[kind="primary"], div.stFormSubmitButton > button[kind="primary"]
    {background:#17624C; color:white; border-color:#17624C;}
    label p, .stRadio p, .stCheckbox p {font-size:17px !important;}
    [data-testid="stMetric"] {background:#E8EFE6; padding:14px; border-radius:14px;}
    [data-testid="stCaptionContainer"] {color:#4A6055;}
    @media(max-width:640px) {.block-container {padding-left:1rem; padding-right:1rem;}}
    </style>""", unsafe_allow_html=True)
    try:
        init_db()
    except (sqlite3.Error, OSError):
        st.error("자료 저장소를 열 수 없어요. 운영자는 DB 경로와 쓰기 권한을 확인해주세요.")
        st.stop()
    if "user_id" not in st.session_state:
        login_screen()
        return
    data, rev = load_data(st.session_state.user_id)
    c1, c2 = st.columns([5, 1])
    c1.title(f"🌱 {data['farm_name']}")
    if c2.button("로그아웃", use_container_width=True):
        st.session_state.clear()
        st.rerun()
    st.caption(f"{today():%Y년 %m월 %d일} · {st.session_state.get('username', '')} 님 · 저장 버튼을 누른 변경은 서버에 저장됩니다.")
    if "flash" in st.session_state:
        st.info(st.session_state.pop("flash"))
    c1, c2, c3 = st.columns(3)
    c1.metric("하우스·밭", f"{len(data['plots'])}곳")
    c2.metric("오늘까지 할 일", f"{sum(not t['done'] and t['due'] <= today().isoformat() for t in data['tasks'])}건")
    c3.metric("오늘 남긴 기록", f"{sum(r['at'][:10] == today().isoformat() for r in data['records'])}건")
    page = st.radio("메뉴", ["🗺️ 내 농장", "✅ 할 일", "🌦️ 날씨", "📦 백업·설정"], horizontal=True, label_visibility="collapsed")
    st.divider()
    try:
        if page == "🗺️ 내 농장":
            farm_page(data, rev)
        elif page == "✅ 할 일":
            st.subheader("✅ 우리 농장 할 일")
            if not data["plots"]:
                st.info("‘내 농장’에서 하우스나 밭을 먼저 등록해주세요.")
            task_list(data, rev)
        elif page == "🌦️ 날씨":
            weather_page(data)
        else:
            backup_page(data, rev)
    except (sqlite3.Error, OSError):
        st.error("저장소에 문제가 생겼어요. 입력 내용을 확인하고 다시 시도해주세요. 계속되면 운영자에게 알려주세요.")
    except ValueError as exc:
        st.error(str(exc))
    st.divider()
    st.caption("내 하우스 · 배치는 참고용, 날씨는 외부 예보입니다. 중요한 기록은 ‘백업·설정’에서 내려받으세요.")


if __name__ == "__main__":
    main()
