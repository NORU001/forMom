"""농사한권 — Streamlit 농사 경영장부
실행: pip install -r requirements.txt && streamlit run app.py
GitHub에는 app.py, requirements.txt만 올린 후 Streamlit Community Cloud에 연결합니다.
선택 Secrets: APP_PASSWORD = "직접 정한 긴 비밀번호"
              APP_URL = "https://배포주소.streamlit.app"
              DB_PATH = "farm_ledger.sqlite3"
주의: SQLite는 현재 실행 서버의 공용 장부입니다. Cloud 재시작/재배포 시
유실될 수 있으므로 CSV 백업 필수. 여러 사용자별 독립 저장/영구 저장을 보장하지 않습니다.
비밀번호 없는 공개 배포는 실제 장부에 사용하지 마세요.
"""
import calendar
import hashlib
import hmac
import html
import io
import os
import sqlite3
import uuid
from datetime import date, datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import qrcode
import requests
import streamlit as st

st.set_page_config(page_title="농사한권 · 농사 경영장부", page_icon="🌱", layout="centered")

def secret(key, default=""):
    try:
        return st.secrets.get(key, default)
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return default

PASSWORD = str(secret("APP_PASSWORD", os.getenv("APP_PASSWORD", "")))
if PASSWORD and not st.session_state.get("authenticated", False):
    st.title("🌱 농사한권")
    st.caption("우리 농장의 경영장부에 로그인하세요.")
    with st.form("login"):
        entered = st.text_input("공유 비밀번호", type="password")
        if st.form_submit_button("장부 열기", use_container_width=True):
            if hmac.compare_digest(entered.encode(), PASSWORD.encode()):
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("비밀번호를 확인해주세요.")
    st.stop()

st.markdown("""<style>
.stApp {background:#f7f8fa;color:#25272c}
.block-container {max-width:850px;padding-top:1.8rem;padding-bottom:3rem}
h1,h2,h3 {letter-spacing:-.045em;color:#25272c}
.brand {color:#e95377;font-size:23px;font-weight:900;margin-bottom:18px}
.hero {background:linear-gradient(115deg,#fff 72%,#ffe4eb);border:1px solid #f2e4e8;
 border-radius:24px;padding:25px;margin:8px 0 18px;box-shadow:0 5px 18px #20202005}
.eyebrow {font-size:13px;color:#797e87}.hero h2 {margin:8px 0;font-size:25px}
.hero p {font-size:14px;color:#626773;margin:0}
.balance {font-weight:850;font-size:32px;letter-spacing:-1px;margin-top:12px}
.tag {display:inline-block;background:#fff0f4;color:#b62e51;border-radius:30px;padding:5px 12px;font-size:12px}
[data-testid="stVerticalBlockBorderWrapper"]>div {border-radius:20px!important;background:white}
[data-testid="stMetric"] {background:white;border-radius:18px;padding:16px;border:1px solid #f0f0f3}
[data-testid="stMetricLabel"] {color:#666c78;font-size:13px}
[data-testid="stMetricValue"] {font-size:24px}
.stButton>button[kind="primary"],.stFormSubmitButton>button[kind="primary"] {background:#e95377;border-color:#e95377;border-radius:14px}
button {border-radius:12px!important}
[data-testid="stHorizontalBlock"] {gap:.7rem}
[data-testid="stSidebar"] {background:#fff}
@media(max-width:640px){.block-container{padding:1rem .9rem 3rem}.hero{padding:21px}.balance{font-size:29px}[data-testid="stMetricValue"]{font-size:20px}}
</style>""", unsafe_allow_html=True)

DB_PATH = str(secret("DB_PATH", os.getenv("DB_PATH", "farm_ledger.sqlite3")))
COLUMNS = ["id", "date", "kind", "category", "crop", "amount", "memo"]
CATS = {"수입": ["농산물 판매", "보조금", "체험·가공", "기타 수입"], "지출": ["종자·종묘", "비료·농약", "인건비", "농기계·수리", "유류·전기", "임차료", "포장·운송", "기타 지출"]}
REGIONS = {"수원":(37.2636,127.0286),"춘천":(37.8813,127.7300),"강릉":(37.7519,128.8761),"청주":(36.6424,127.4890),"충주":(36.9910,127.9259),"천안":(36.8151,127.1139),"홍성":(36.6012,126.6608),"전주":(35.8242,127.1480),"김제":(35.8036,126.8809),"나주":(35.0160,126.7108),"해남":(34.5733,126.5993),"상주":(36.4109,128.1590),"안동":(36.5684,128.7294),"진주":(35.1800,128.1076),"밀양":(35.5038,128.7464),"제주":(33.4996,126.5312),"서귀포":(33.2541,126.5601)}

def connect():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.execute("PRAGMA busy_timeout=20000")
    return con

with connect() as con:
    con.execute("""CREATE TABLE IF NOT EXISTS entries (
    id TEXT PRIMARY KEY, date TEXT NOT NULL, kind TEXT NOT NULL,
    category TEXT NOT NULL, crop TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>0), memo TEXT NOT NULL)""")

def read_entries():
    with connect() as con:
        return pd.read_sql_query("SELECT * FROM entries ORDER BY date DESC, rowid DESC", con)

def add_entry(day, kind, category, crop, amount, memo):
    with connect() as con:
        con.execute("INSERT INTO entries VALUES (?,?,?,?,?,?,?)", (uuid.uuid4().hex,day.isoformat(),kind,category,crop.strip(),int(amount),memo.strip()))

def won(value):
    return f"{int(value):,}원"

def csv_bytes(df):
    # Spreadsheet formula injection 방지. 복원 시 아래 접두어를 제거합니다.
    safe = df.copy()
    for col in ["category", "crop", "memo"]:
        safe[col] = safe[col].map(lambda x: "'"+x if isinstance(x,str) and x.startswith(("=","+","-","@","\t","\r")) else x)
    return safe.to_csv(index=False).encode("utf-8-sig")

def validate_import(upload):
    frame = pd.read_csv(upload, dtype=str, keep_default_na=False)
    if set(frame.columns) != set(COLUMNS):
        raise ValueError("이 앱에서 내려받은 전체 백업 CSV만 복원할 수 있습니다.")
    if len(frame)>50000:
        raise ValueError("한 번에 최대 50,000건까지 복원할 수 있습니다.")
    rows=[]
    for row in frame[COLUMNS].itertuples(index=False, name=None):
        ident, day, kind, category, crop, amount, memo=row
        if len(ident)!=32 or any(c not in "0123456789abcdef" for c in ident):
            raise ValueError("장부 ID 형식이 올바르지 않습니다.")
        if date.fromisoformat(day).isoformat()!=day or kind not in CATS:
            raise ValueError("날짜 또는 수입·지출 구분이 올바르지 않습니다.")
        if not amount.isdigit() or not 0<int(amount)<=100_000_000_000:
            raise ValueError("금액은 1원~1,000억원의 정수여야 합니다.")
        for name,value in [("항목",category),("작물",crop),("메모",memo)]:
            if len(value)>1000:
                raise ValueError(f"{name} 내용이 너무 깁니다.")
        category,crop,memo=[v[1:] if len(v)>1 and v[0]=="'" and v[1] in "=+-@\t\r" else v for v in (category,crop,memo)]
        if category not in CATS[kind]:
            raise ValueError("알 수 없는 장부 항목이 있습니다.")
        rows.append((ident,day,kind,category,crop,int(amount),memo))
    return rows

@st.cache_data(ttl=1800, show_spinner=False)
def weather(lat, lon):
    response=requests.get("https://api.open-meteo.com/v1/forecast",params={
        "latitude":lat,"longitude":lon,"daily":"weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max",
        "timezone":"Asia/Seoul","forecast_days":7},timeout=15)
    response.raise_for_status()
    return response.json(), datetime.now(ZoneInfo("Asia/Seoul")).strftime("%m.%d %H:%M")

def weather_label(code):
    if code==0:return "☀️ 맑음"
    if code in (1,2,3):return "⛅ 구름"
    if code in (45,48):return "🌫️ 안개"
    if code in (71,73,75,77,85,86):return "❄️ 눈"
    if code in (95,96,99):return "⛈️ 뇌우"
    if code in (51,53,55,56,57,61,63,65,66,67,80,81,82):return "🌧️ 비"
    return "🌤️ 예보 확인"

now=date.today()
with st.sidebar:
    st.subheader("🌱 우리 농장 설정")
    farm=st.text_input("농장 이름", "우리 농장",max_chars=40)
    region=st.selectbox("날씨 지역",list(REGIONS),index=8)
    custom=st.checkbox("농장 좌표 직접 지정")
    lat,lon=REGIONS[region]
    if custom:
        lat=st.number_input("위도",-90.0,90.0,float(lat),format="%.5f")
        lon=st.number_input("경도",-180.0,180.0,float(lon),format="%.5f")
    st.caption("설정은 현재 세션에 적용됩니다. 지역 대표 좌표 예보는 농장 현장과 다를 수 있습니다.")
    st.divider()
    st.caption("이 버전은 동일 서버의 공용 장부입니다. 농장별 계정 분리는 지원하지 않습니다.")
    if PASSWORD and st.button("로그아웃"):
        st.session_state.authenticated=False
        st.rerun()

st.markdown('<div class="brand">농사한권 ▪</div>',unsafe_allow_html=True)
if not PASSWORD:
    st.warning("체험 모드 · 접근 비밀번호가 없습니다. 실제 장부를 공개 배포하기 전 Secrets의 APP_PASSWORD를 설정하세요.")
all_df=read_entries()
nav=st.radio("메뉴",["홈","장부 입력","이용 내역","날씨","QR·백업"],horizontal=True,label_visibility="collapsed")

if nav=="홈":
    month_key=now.strftime("%Y-%m")
    current=all_df[all_df.date.str.startswith(month_key)]
    income=int(current.loc[current.kind=="수입","amount"].sum())
    expense=int(current.loc[current.kind=="지출","amount"].sum())
    st.markdown(f'<div class="hero"><span class="tag">{now.month}월 농장 리포트</span><h2>{html.escape(farm)}의 한 달</h2><p>수입과 지출을 한눈에, 오늘의 농사를 차곡차곡.</p><div class="balance">{won(income-expense)}</div><p>이번 달 장부상 수지 · 입력된 거래 기준</p></div>',unsafe_allow_html=True)
    a,b=st.columns(2)
    a.metric("이번 달 수입",won(income));b.metric("이번 달 지출",won(expense))
    with st.container(border=True):
        st.subheader("최근 장부 내역 ›")
        if all_df.empty:st.info("아직 장부가 비어 있어요. ‘장부 입력’에서 첫 기록을 남겨보세요.")
        for r in all_df.head(4).itertuples():
            a,b=st.columns([3,1])
            a.markdown(f"**{html.escape(r.category)}** · {html.escape(r.crop) or '공통'}")
            a.caption(f"{r.date} · {r.kind}")
            b.markdown(f"**{'+' if r.kind=='수입' else '−'}{won(r.amount)}**")
    with st.container(border=True):
        st.subheader("이번 달, 어디에 가장 많이 썼을까?")
        grouped=current[current.kind=="지출"].groupby("category").amount.sum().sort_values(ascending=False)
        if grouped.empty:st.caption("지출을 기록하면 항목별 비중을 알려드려요.")
        else:
            for category,amount in grouped.items():
                st.write(f"{category} · {won(amount)}")
                st.progress(float(amount/expense),text=f"전체 지출의 {amount/expense:.0%}")
    st.caption("날씨 메뉴에서 농장 지역의 7일 예보와 작업 참고 정보를 확인하세요.")

elif nav=="장부 입력":
    st.header("오늘의 농사, 한 줄 기록")
    kind=st.radio("구분",["지출","수입"],horizontal=True)
    with st.form("entry",clear_on_submit=True):
        day=st.date_input("거래일",value=now)
        category=st.selectbox("항목",CATS[kind])
        crop=st.text_input("작물 / 필지",placeholder="예: 딸기 · 1번 하우스",max_chars=100)
        amount=st.number_input("금액 (원)",min_value=1,max_value=100_000_000_000,value=10000,step=1000)
        memo=st.text_area("메모",placeholder="예: 모종 구입, 판매처, 작업 내용",max_chars=1000)
        submitted=st.form_submit_button("장부에 저장",type="primary",use_container_width=True)
        if submitted:
            add_entry(day,kind,category,crop,amount,memo)
            st.success("장부에 저장했습니다. 홈과 이용 내역에서 확인하세요.")
    st.caption("금액은 부가세 포함 등 농장에서 정한 동일 기준으로 기록하세요. 세무 신고용 자동 계산은 제공하지 않습니다.")

elif nav=="이용 내역":
    st.header("우리 농장 장부")
    a,b=st.columns(2)
    start=a.date_input("시작일",now.replace(day=1))
    end=b.date_input("종료일",now.replace(day=calendar.monthrange(now.year,now.month)[1]))
    if start>end:
        st.error("종료일은 시작일 이후여야 합니다.");st.stop()
    kind_filter=st.selectbox("구분",["전체","수입","지출"])
    keyword=st.text_input("작물·항목·메모 검색",max_chars=100)
    df=all_df[(all_df.date>=start.isoformat())&(all_df.date<=end.isoformat())].copy()
    if kind_filter!="전체":df=df[df.kind==kind_filter]
    if keyword:
        df=df[df[["crop","category","memo"]].apply(lambda col:col.str.contains(keyword,case=False,regex=False)).any(axis=1)]
    income=int(df.loc[df.kind=="수입","amount"].sum());expense=int(df.loc[df.kind=="지출","amount"].sum())
    a,b,c=st.columns(3);a.metric("수입",won(income));b.metric("지출",won(expense));c.metric("수지",won(income-expense))
    display=df.drop(columns="id").rename(columns={"date":"거래일","kind":"구분","category":"항목","crop":"작물·필지","amount":"금액(원)","memo":"메모"})
    st.dataframe(display,hide_index=True,use_container_width=True)
    st.caption(f"조회 결과 {len(df):,}건 · 수지는 현재 조회 조건 기준입니다.")
    st.download_button("조회 결과 CSV",csv_bytes(df),file_name="farm_filtered.csv",mime="text/csv")
    with st.expander("기록 수정·삭제"):
        if df.empty:st.caption("조회된 기록이 없습니다.")
        else:
            mapping={r.id:f"{r.date} · {r.kind} · {r.category} · {won(r.amount)} · {r.id[:6]}" for r in df.itertuples()}
            selected=st.selectbox("대상 기록",list(mapping),format_func=mapping.get)
            row=df[df.id==selected].iloc[0]
            with st.form("edit_"+selected):
                edit_day=st.date_input("거래일 수정",date.fromisoformat(row.date))
                edit_category=st.selectbox("항목 수정",CATS[row.kind],index=CATS[row.kind].index(row.category))
                edit_crop=st.text_input("작물·필지 수정",row.crop,max_chars=100)
                edit_amount=st.number_input("금액 수정",1,100_000_000_000,int(row.amount),step=1000)
                edit_memo=st.text_area("메모 수정",row.memo,max_chars=1000)
                if st.form_submit_button("수정 저장"):
                    with connect() as con:con.execute("UPDATE entries SET date=?,category=?,crop=?,amount=?,memo=? WHERE id=?",(edit_day.isoformat(),edit_category,edit_crop.strip(),int(edit_amount),edit_memo.strip(),selected))
                    st.rerun()
            confirm=st.checkbox("위 기록의 영구 삭제를 확인합니다.")
            if st.button("선택 기록 삭제",disabled=not confirm):
                with connect() as con:con.execute("DELETE FROM entries WHERE id=?",(selected,))
                st.rerun()

elif nav=="날씨":
    st.header("농사 준비는 날씨부터")
    st.caption(f"{'직접 지정한 농장' if custom else region} · 위도 {lat:.4f}, 경도 {lon:.4f}")
    if st.button("예보 새로고침"):
        weather.clear()
    try:
        with st.spinner("7일 예보를 불러오는 중입니다…"):
            data,updated=weather(lat,lon)
        d=data["daily"]
        st.markdown(f"### {weather_label(d['weather_code'][0])}")
        a,b,c=st.columns(3)
        a.metric("오늘 최저 / 최고",f"{d['temperature_2m_min'][0]} / {d['temperature_2m_max'][0]}℃")
        b.metric("강수확률",f"{d['precipitation_probability_max'][0]}%")
        c.metric("최대 풍속",f"{d['wind_speed_10m_max'][0]} km/h")
        if d['temperature_2m_min'][0] is not None and d['temperature_2m_min'][0]<=2:
            st.warning("저온 주의 참고: 최저기온이 2℃ 이하입니다. 작물별 보온·서리 피해 가능성을 확인하세요.")
        if (d['precipitation_probability_max'][0] or 0)>=60:
            st.info("비 가능성이 높습니다. 배수로를 점검하고 방제·야외작업 일정을 재확인하세요.")
        if (d['wind_speed_10m_max'][0] or 0)>=25:
            st.warning("강풍 주의 참고: 시설물 고정과 농약 비산 가능성을 확인하세요.")
        forecast=pd.DataFrame({"날짜":d["time"],"날씨":[weather_label(x) for x in d["weather_code"]],"최저(℃)":d["temperature_2m_min"],"최고(℃)":d["temperature_2m_max"],"강수확률(%)":d["precipitation_probability_max"],"강수량(mm)":d["precipitation_sum"],"최대풍속(km/h)":d["wind_speed_10m_max"]})
        st.dataframe(forecast,hide_index=True,use_container_width=True)
        st.caption(f"조회 시각(KST): {updated} · 최대 30분 캐시. 작업 참고 알림은 자체 기준이며 공식 기상특보가 아닙니다.")
    except (requests.RequestException,KeyError,ValueError,TypeError) as exc:
        st.warning("날씨 정보를 불러오지 못했습니다. 잠시 후 새로고침하거나 지역·좌표를 확인하세요.")
    st.markdown("예보: [Open-Meteo](https://open-meteo.com/) · [라이선스 CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) · [공식 기상특보 확인](https://www.weather.go.kr/)")
    st.caption("기상 예보는 현장과 다를 수 있습니다. 상업적 운영 전 Open-Meteo 이용 조건·요금과 호출 제한을 확인하세요.")

else:
    st.header("QR로 열고, 백업으로 지키기")
    with st.container(border=True):
        st.subheader("📱 우리 농장 접속 QR")
        url=st.text_input("배포한 앱의 HTTPS 주소",value=str(secret("APP_URL","")),placeholder="https://your-farm.streamlit.app")
        parsed=urlparse(url.strip())
        if url:
            if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password:
                st.error("정상적인 HTTPS 배포 주소를 입력해주세요.")
            else:
                image=qrcode.make(url.strip());buffer=io.BytesIO();image.save(buffer,format="PNG")
                st.image(buffer.getvalue(),width=220,caption="휴대폰 카메라로 스캔하면 배포한 앱이 열립니다.")
                st.download_button("QR 이미지 다운로드",buffer.getvalue(),file_name="farm_qr.png",mime="image/png")
        st.caption("QR은 접속 주소만 담습니다. 로그인 우회나 장부 데이터 자체를 포함하지 않습니다. 스캔한 휴대폰에서 로그인 비밀번호를 입력해야 합니다.")
    with st.container(border=True):
        st.subheader("💾 전체 장부 백업")
        st.warning("현재 저장 방식은 서버 로컬 SQLite입니다. Streamlit Cloud 재시작·재배포 시 데이터가 사라질 수 있습니다. 입력 후 CSV를 꼭 내려받으세요. 영구 운영에는 별도 외부 DB 연결이 필요합니다.")
        st.download_button(f"전체 {len(all_df):,}건 백업 CSV",csv_bytes(all_df),file_name=f"farm_backup_{now.isoformat()}.csv",mime="text/csv",type="primary")
        upload=st.file_uploader("백업 CSV 복원",type=["csv"])
        st.caption("동일 ID는 건너뛰며 기존 기록을 덮어쓰지 않습니다. 삭제한 기록은 이전 백업 복원 시 다시 추가됩니다.")
        if upload is not None:
            if upload.size>20*1024*1024:
                st.error("20MB 이하 CSV를 선택하세요.")
            else:
                try:
                    rows=validate_import(upload)
                    st.info(f"복원 가능한 기록: {len(rows):,}건")
                    if st.button("기존 장부에 추가 복원"):
                        with connect() as con:
                            before=con.total_changes
                            con.executemany("INSERT OR IGNORE INTO entries VALUES (?,?,?,?,?,?,?)",rows)
                            inserted=con.total_changes-before
                        st.success(f"{inserted:,}건 추가했습니다. 다른 메뉴에서 확인하세요.")
                except (ValueError,pd.errors.ParserError,UnicodeDecodeError) as exc:
                    st.error(f"복원할 수 없습니다: {exc}")
    with st.expander("GitHub → Streamlit 배포 안내"):
        st.markdown('''1. GitHub 저장소에 **app.py**, **requirements.txt** 두 파일을 올립니다. CSV·SQLite 파일은 올리지 마세요.
2. Streamlit Community Cloud에서 저장소를 연결하고 Main file path를 `app.py`로 지정합니다.
3. 앱 설정의 Secrets에 아래 값을 입력합니다. 비밀번호를 GitHub 코드에 쓰지 마세요.
4. 배포 후 QR 메뉴에 HTTPS 주소를 입력하거나 Secrets의 APP_URL에 저장합니다.
5. 실제 장부 입력 후 CSV 백업을 내려받아 보관합니다.''')
        st.code('APP_PASSWORD = "충분히 긴 농장 공용 비밀번호"\nAPP_URL = "https://your-farm.streamlit.app"',language="toml")
        st.caption("이 앱은 1개 농장의 공유 장부용입니다. QR만으로 누구나 장부를 보지 않도록 비밀번호를 꼭 설정하세요.")

st.caption("농사한권 · 오늘의 기록이 내일의 농장을 만듭니다 🌱")
