"""장보기 알림메모
실행: streamlit run app.py
GitHub에 app.py와 requirements.txt를 올리고 Streamlit Community Cloud에서 배포.
QR은 배포 주소를 여는 용도이며 다른 기기와 목록을 자동 공유하지 않습니다.
세션 데이터는 영구 저장되지 않습니다. JSON 백업/복원을 이용하세요.
"""
import json
import uuid
from io import BytesIO
from urllib.parse import urlparse

import qrcode
import streamlit as st

st.set_page_config(page_title="장보기 알림메모", page_icon="🛒", layout="centered", initial_sidebar_state="collapsed")
st.markdown("""<style>
header[data-testid="stHeader"]{display:none}
.block-container{max-width:520px;padding:.5rem .6rem!important}
[data-testid="stVerticalBlock"]{gap:.3rem}
h3{font-size:1.25rem!important;padding:0!important;margin:0!important}
.stButton button{min-height:2.25rem;padding:.15rem .35rem}
[data-testid="stMetricValue"]{font-size:1.45rem}
[data-testid="stCaptionContainer"]{font-size:.75rem}
[data-testid="stDialog"] [role="dialog"]{max-height:90dvh;overflow-y:auto}
</style>""", unsafe_allow_html=True)
CAT = ["🥬 야채", "🍎 과일", "🥩 육류", "🐟 수산물", "🥛 유제품·달걀", "🍚 식재료", "🧊 냉동·간편식", "🧻 생활용품", "📦 기타"]
PRI = {1: "🔴 1 꼭 필요", 2: "🟡 2 가격 보고", 3: "🟢 3 저렴하면"}
for k, v in {"notes": [], "cart": []}.items():
    if k not in st.session_state:
        st.session_state[k] = v

def won(n):
    return f"{n:,}원"

def total():
    return sum(x["price"] * x["qty"] for x in st.session_state.cart)

def get_note(i):
    return next((x for x in st.session_state.notes if x["id"] == i), None)

@st.dialog("🛒 가격·수량 확인")
def buy(i):
    x = get_note(i)
    if x is None:
        st.info("이미 처리된 상품입니다.")
        return
    st.text(x["name"])
    st.caption(PRI[x["priority"]] + " · 가격은 1봉/1팩/1개 기준 단가입니다.")
    p = int(st.number_input("구매 단가 (원)", min_value=0, max_value=100000000, step=100, key=f"p_{i}"))
    q = int(st.number_input("수량 (봉/팩/개)", min_value=1, max_value=10000, step=1, key=f"q_{i}"))
    st.info(f"상품 합계 {won(p*q)} · 담은 뒤 카트 {won(total()+p*q)}")
    over = bool(x["limit"] and p > x["limit"])
    ok = True
    if over:
        st.warning(f"희망 단가 상한 {won(x['limit'])} 초과! 비싸면 보류하세요.")
        ok = st.checkbox("상한 초과를 확인했고 구매합니다", key=f"ok_{i}")
    elif x["priority"] > 1:
        st.caption("필수 상품이 아니에요. 가격이 부담되면 보류하세요.")
    a, b = st.columns(2)
    if a.button("✅ 구매 확정", disabled=not ok, use_container_width=True):
        if get_note(i) is not None:
            st.session_state.cart.append({**x, "price": p, "qty": q})
            st.session_state.notes = [n for n in st.session_state.notes if n["id"] != i]
        st.rerun()
    if b.button("비싸서 보류", use_container_width=True):
        x["held"] = True
        st.rerun()

@st.dialog("✏️ 메모 수정")
def edit(i):
    x = get_note(i)
    if x is None:
        return
    with st.form("edit"):
        name = st.text_input("상품명", value=x["name"], max_chars=60)
        cat = st.selectbox("카테고리", CAT, index=CAT.index(x["category"]))
        priority = st.selectbox("중요도", [1, 2, 3], index=x["priority"]-1, format_func=lambda n: PRI[n])
        limit = st.number_input("희망 단가 상한 (0=미설정)", min_value=0, max_value=100000000, value=x["limit"], step=100)
        saved = st.form_submit_button("저장", use_container_width=True)
    if saved:
        if not name.strip():
            st.error("상품명을 입력하세요.")
        else:
            x.update(name=name.strip(), category=cat, priority=priority, limit=int(limit))
            st.rerun()
    if st.button("메모 삭제 (구매 아님)", use_container_width=True):
        st.session_state.notes = [n for n in st.session_state.notes if n["id"] != i]
        st.rerun()

def validate(data):
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("지원하지 않는 백업입니다.")
    notes, cart = data.get("notes"), data.get("cart")
    if not isinstance(notes, list) or not isinstance(cart, list) or len(notes)+len(cart)>3000:
        raise ValueError("목록 형식/개수를 확인하세요.")
    ids = set()
    for x in notes + cart:
        if not isinstance(x, dict):
            raise ValueError("상품 형식 오류")
        i = x.get("id")
        if not isinstance(i, str) or not 1 <= len(i) <= 100 or i in ids:
            raise ValueError("상품 ID 오류")
        ids.add(i)
        if not isinstance(x.get("name"), str) or not x["name"].strip() or len(x["name"])>60:
            raise ValueError("상품명 오류")
        if x.get("category") not in CAT or type(x.get("priority")) is not int or x["priority"] not in PRI:
            raise ValueError("카테고리/중요도 오류")
        if type(x.get("held")) is not bool or type(x.get("limit")) is not int or not 0<=x["limit"]<=100000000:
            raise ValueError("상태/상한 오류")
    for x in cart:
        if type(x.get("price")) is not int or not 0<=x["price"]<=100000000 or type(x.get("qty")) is not int or not 1<=x["qty"]<=10000:
            raise ValueError("가격/수량 오류")
    return notes, cart

@st.dialog("💾 백업 · QR 접속")
def tools():
    st.caption("현재 브라우저 세션에만 저장됩니다. 새로고침·접속 종료 시 사라질 수 있으니 백업하세요. QR은 앱 접속용으로, 목록 공유 기능은 아닙니다.")
    data = {"version": 1, "notes": st.session_state.notes, "cart": st.session_state.cart}
    st.download_button("장보기 백업 다운로드", json.dumps(data, ensure_ascii=False, indent=2), "shopping.json", "application/json", use_container_width=True)
    f = st.file_uploader("JSON 백업 복원", type="json")
    confirmed = st.checkbox("현재 목록을 백업 내용으로 교체합니다")
    if st.button("복원", disabled=f is None or not confirmed):
        try:
            if f.size > 2000000:
                raise ValueError("2MB 이하 파일만 가능합니다.")
            notes, cart = validate(json.loads(f.getvalue()))
            st.session_state.notes, st.session_state.cart = notes, cart
            st.rerun()
        except (ValueError, TypeError, KeyError) as exc:
            st.error(f"복원 실패: {exc}")
    st.divider()
    url = st.text_input("배포된 앱 주소", placeholder="https://앱이름.streamlit.app")
    if url:
        u = urlparse(url.strip())
        if u.scheme == "https" and u.netloc and len(url)<1500:
            buf = BytesIO()
            qrcode.make(url.strip()).save(buf, format="PNG")
            st.image(buf.getvalue(), width=170)
            st.download_button("QR 이미지 다운로드", buf.getvalue(), "shopping_qr.png", "image/png")
        else:
            st.warning("https://로 시작하는 배포 주소를 입력하세요.")

h1, h2 = st.columns([3, 1])
h1.markdown("### 🛒 장보기 알림메모")
if h2.button("💾 QR", use_container_width=True):
    tools()
count = sum(x["qty"] for x in st.session_state.cart)
st.metric(f"현재 카트 · {len(st.session_state.cart)}종 / {count}개", won(total()))
with st.form("add", clear_on_submit=True):
    a, b = st.columns([1, 1.5])
    category = a.selectbox("카테고리", CAT, label_visibility="collapsed")
    name = b.text_input("상품명", placeholder="상품명 직접 입력", max_chars=60, label_visibility="collapsed")
    a, b, c = st.columns([1.3, 1.1, .65])
    priority = a.selectbox("중요도", [1, 2, 3], format_func=lambda n: PRI[n], label_visibility="collapsed")
    limit = b.number_input("희망 단가 상한 (0=없음)", min_value=0, max_value=100000000, step=100, label_visibility="collapsed", help="희망 단가 상한(원). 0은 미설정입니다.")
    added = c.form_submit_button("추가", use_container_width=True)
if added:
    if not name.strip():
        st.warning("상품명을 입력하세요.")
    else:
        st.session_state.notes.append({"id": uuid.uuid4().hex, "name": name.strip(), "category": category, "priority": priority, "limit": int(limit), "held": False})
        st.rerun()
st.caption("🔴 1 필수 · 🟡 2 가격 보고 · 🟢 3 저렴하면 | 숫자 입력: 희망 단가 상한(0=없음)")
a, b = st.columns(2)
view = a.selectbox("보기", ["살 것", "보류", "카트"], label_visibility="collapsed")
filt = b.selectbox("카테고리 필터", ["전체"]+CAT, label_visibility="collapsed")
rows = list(reversed(st.session_state.cart)) if view == "카트" else sorted([x for x in st.session_state.notes if x["held"] == (view == "보류")], key=lambda x: x["priority"])
if filt != "전체":
    rows = [x for x in rows if x["category"] == filt]
# 목록만 내부 스크롤: 항목 수가 늘어도 본문 전체가 길어지지 않습니다.
with st.container(height=245, border=True):
    if not rows:
        st.caption("목록이 비었어요. 상품을 추가하거나 필터를 바꿔보세요.")
    for x in rows:
        short = x["name"] if len(x["name"])<=24 else x["name"][:23]+"…"
        st.text(f"{PRI[x['priority']].split()[0]} {short}")
        if view == "카트":
            a, b = st.columns([2, 1])
            a.caption(f"{won(x['price'])} × {x['qty']} = {won(x['price']*x['qty'])}")
            if b.button("담기 취소", key=f"undo_{x['id']}", use_container_width=True):
                note = {k: x[k] for k in ["id", "name", "category", "priority", "limit", "held"]}
                note["held"] = False
                st.session_state.notes.append(note)
                st.session_state.cart = [n for n in st.session_state.cart if n["id"] != x["id"]]
                st.rerun()
        else:
            cap = f" · 상한 {won(x['limit'])}" if x["limit"] else ""
            st.caption(x["category"]+cap)
            a, b, c = st.columns([1.2, 1, 1])
            if a.button("🛒 담기", key=f"buy_{x['id']}", use_container_width=True):
                buy(x["id"])
            if b.button("복원" if x["held"] else "보류", key=f"hold_{x['id']}", use_container_width=True):
                x["held"] = not x["held"]
                st.rerun()
            if c.button("수정", key=f"edit_{x['id']}", use_container_width=True):
                edit(x["id"])
        st.divider()
