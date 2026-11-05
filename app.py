"""우리 가족 캘린더 — 자체 회원가입/로그인 + PostgreSQL

GitHub에 app.py와 requirements.txt 업로드 후 Streamlit Community Cloud에서 배포.
Google Cloud/OAuth 설정은 전혀 필요하지 않습니다.
Streamlit 앱 Settings > Secrets에는 다음 두 값만 입력하세요:

APP_URL = "https://your-app.streamlit.app"
DATABASE_URL = "postgresql://USER:PASSWORD@HOST:5432/DB?sslmode=require"

Neon 또는 Supabase에서 PostgreSQL 연결 문자열을 발급받으세요.
Supabase는 IPv4 호환 pooler 연결 문자열을 권장합니다.
공유 일정의 영구 저장을 위해 DB 연결은 반드시 필요합니다.
Secrets는 GitHub에 올리지 마세요. 앱 전용 DB/사용자를 사용하세요.
최초 실행 시 테이블 자동 생성 (CREATE 권한 필요).
로컬: pip install -r requirements.txt && streamlit run app.py
로컬 설정: .streamlit/secrets.toml (Git 제외), APP_URL=http://localhost:8501
QR을 다른 휴대폰에서 사용하려면 공개 HTTPS 배포 주소가 필요합니다.

자체 아이디는 영문/숫자/밑줄 4~30자, 비밀번호는 10~128자.
비밀번호는 16바이트 무작위 salt + PBKDF2-HMAC-SHA256 600,000회 해시로 저장.
아이디별 5회 로그인 실패 시 5분 잠금. 방 입장도 세션 내 속도 제한 적용.
로그인은 현재 탭의 서버 세션에서 최대 12시간 유지됩니다. 새로고침/재접속 시
재로그인이 필요할 수 있습니다. 자동 로그인/비밀번호 찾기/이메일 인증 미지원.
이전 Google 계정의 데이터는 유지되지만 새 계정과 자동 연결하지 않습니다.
새 계정으로 가입한 후 새 방을 만들거나 기존 방의 초대코드를 사용하세요.
시계는 1초, 공유 일정은 10초 폴링. 시간대는 Asia/Seoul.
초대코드/QR은 비공개로 공유하세요. 같은 방의 가족은 이름과 일정을 봅니다.
공개 대규모 서비스 전환 시 별도 계정 복구, CAPTCHA, 요청 제한을 보강하세요.
"""
from __future__ import annotations

import calendar
import hashlib
import hmac
import re
import time as systime
from datetime import date, datetime, time, timedelta
import io
import logging
import secrets
import string
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
import qrcode
import streamlit as st

st.set_page_config(page_title="우리 가족 캘린더", page_icon="📅", layout="wide")
TZ = ZoneInfo("Asia/Seoul")
LOG = logging.getLogger(__name__)


class UserError(Exception):
    pass


def connect():
    return psycopg.connect(st.secrets["DATABASE_URL"], row_factory=dict_row,
                           connect_timeout=10, prepare_threshold=None)


@st.cache_resource
def initialize():
    # CREATE 권한은 첫 실행에 필요합니다. 운영에서는 관리자가 미리 생성해도 됩니다.
    with connect() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS fc_users (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fc_credentials (
          username TEXT PRIMARY KEY,
          user_id TEXT UNIQUE NOT NULL REFERENCES fc_users(id),
          password_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fc_login_guard (
          username TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
          locked_until TIMESTAMPTZ);
        CREATE TABLE IF NOT EXISTS fc_rooms (
          id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL,
          owner_id TEXT NOT NULL REFERENCES fc_users(id),
          code VARCHAR(7) UNIQUE NOT NULL, capacity INTEGER NOT NULL
            CHECK (capacity BETWEEN 1 AND 100),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now());
        CREATE TABLE IF NOT EXISTS fc_members (
          room_id BIGINT NOT NULL REFERENCES fc_rooms(id) ON DELETE CASCADE,
          user_id TEXT NOT NULL REFERENCES fc_users(id),
          joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY(room_id,user_id));
        CREATE TABLE IF NOT EXISTS fc_events (
          id BIGSERIAL PRIMARY KEY,
          room_id BIGINT NOT NULL REFERENCES fc_rooms(id) ON DELETE CASCADE,
          author_id TEXT NOT NULL REFERENCES fc_users(id),
          title TEXT NOT NULL, starts_at TIMESTAMPTZ NOT NULL,
          ends_at TIMESTAMPTZ NOT NULL, location TEXT NOT NULL DEFAULT '',
          notes TEXT NOT NULL DEFAULT '',
          CHECK (ends_at > starts_at));
        CREATE INDEX IF NOT EXISTS fc_events_room_time
          ON fc_events(room_id, starts_at);
        """)
    return True



def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 600000)
    return f"600000${salt.hex()}${digest.hex()}"


def password_matches(password, encoded):
    try:
        rounds, salt, expected = encoded.split('$')
        actual = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                    bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def register(username, display_name, password, confirmation):
    username = username.strip().lower()
    if not re.fullmatch(r'[a-z0-9_]{4,30}', username):
        raise UserError("아이디는 영문/숫자/밑줄 4~30자로 입력하세요.")
    if not display_name.strip() or len(display_name.strip()) > 40:
        raise UserError("이름은 1~40자로 입력하세요.")
    if not 10 <= len(password) <= 128:
        raise UserError("비밀번호는 10~128자로 입력하세요.")
    if password != confirmation:
        raise UserError("비밀번호 확인이 일치하지 않습니다.")
    encoded = password_hash(password)
    uid = 'local:' + secrets.token_hex(16)
    try:
        with connect() as c:
            c.execute("INSERT INTO fc_users(id,name,email) VALUES (%s,%s,%s)",
                      (uid, display_name.strip(), ''))
            c.execute("INSERT INTO fc_credentials(username,user_id,password_hash) VALUES (%s,%s,%s)",
                      (username, uid, encoded))
    except psycopg.errors.UniqueViolation:
        raise UserError("이미 사용 중인 아이디입니다.") from None
    return uid


def authenticate(username, password):
    username = username.strip().lower()
    if not re.fullmatch(r'[a-z0-9_]{4,30}', username) or len(password) > 128:
        return None
    with connect() as c:
        c.execute("INSERT INTO fc_login_guard(username) VALUES (%s) ON CONFLICT DO NOTHING", (username,))
        guard = c.execute("SELECT * FROM fc_login_guard WHERE username=%s FOR UPDATE", (username,)).fetchone()
        now = datetime.now(TZ)
        if guard['locked_until'] and guard['locked_until'] > now:
            return None
        record = c.execute("SELECT * FROM fc_credentials WHERE username=%s", (username,)).fetchone()
        # Unknown users perform the same expensive hash, without disclosing account existence.
        encoded = record['password_hash'] if record else '600000$' + ('00' * 16) + '$' + ('00' * 32)
        valid = password_matches(password, encoded)
        if valid and record:
            c.execute("UPDATE fc_login_guard SET failures=0,locked_until=NULL WHERE username=%s", (username,))
            return record['user_id']
        failures = (0 if guard['locked_until'] else guard['failures']) + 1
        until = now + timedelta(minutes=5) if failures >= 5 else None
        c.execute("UPDATE fc_login_guard SET failures=%s,locked_until=%s WHERE username=%s",
                  (failures, until, username))
    return None


def auth_screen():
    st.subheader("가족의 시간을 한곳에")
    st.write("Google 계정 없이, 이 앱에서 만든 아이디로 이용하세요.")
    pending = st.session_state.get('pending_invite')
    if pending:
        st.info(f"초대코드 {pending} · 로그인 후 왼쪽 메뉴에서 입장해 주세요.")
    login_tab, signup_tab = st.tabs(["로그인", "회원가입"])
    with login_tab:
        with st.form('local_login', clear_on_submit=True):
            username = st.text_input("아이디", max_chars=30)
            password = st.text_input("비밀번호", type='password', max_chars=128)
            submitted = st.form_submit_button("로그인", type='primary')
        if submitted:
            last = st.session_state.get('last_login_attempt', 0)
            if systime.monotonic() - last < 2:
                st.warning("잠시 후 다시 시도해 주세요.")
            else:
                st.session_state['last_login_attempt'] = systime.monotonic()
                ok, uid = attempt(authenticate, username, password)
                if ok and uid:
                    st.session_state['local_uid'] = uid
                    st.session_state['login_at'] = systime.time()
                    st.rerun()
                elif ok:
                    st.error("로그인할 수 없습니다. 아이디·비밀번호를 확인하세요. 5회 실패했다면 5분 후 다시 시도하세요.")
    with signup_tab:
        with st.form('local_signup', clear_on_submit=True):
            username = st.text_input("새 아이디 (영문/숫자/밑줄 4~30자)", max_chars=30)
            display_name = st.text_input("가족에게 보여줄 이름", max_chars=40)
            password = st.text_input("새 비밀번호 (10자 이상)", type='password', max_chars=128)
            confirmation = st.text_input("비밀번호 확인", type='password', max_chars=128)
            consent = st.checkbox("아이디·이름·비밀번호 해시 및 일정 저장에 동의합니다.")
            submitted = st.form_submit_button("회원가입", type='primary')
        if submitted:
            if not consent:
                st.warning("저장 안내에 동의해 주세요.")
            elif systime.monotonic() - st.session_state.get('last_signup_attempt', 0) < 5:
                st.warning("잠시 후 다시 시도해 주세요.")
            else:
                st.session_state['last_signup_attempt'] = systime.monotonic()
                ok, uid = attempt(register, username, display_name, password, confirmation)
                if ok:
                    st.session_state['local_uid'] = uid
                    st.session_state['login_at'] = systime.time()
                    st.rerun()
    st.caption("비밀번호 찾기는 지원하지 않으니 안전하게 보관하세요. 새로고침/재접속 시 재로그인이 필요할 수 있습니다.")


def code7():
    return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(7))


def room_for(c, rid, uid, lock=False):
    # Room lock serializes membership/capacity changes and event authorizations.
    room = c.execute("SELECT * FROM fc_rooms WHERE id=%s" +
                     (" FOR UPDATE" if lock else ""), (rid,)).fetchone()
    if not room or not c.execute(
        "SELECT 1 FROM fc_members WHERE room_id=%s AND user_id=%s", (rid, uid)
    ).fetchone():
        raise UserError("방에 접근할 권한이 없습니다. 방 목록을 새로고침해 주세요.")
    return room


def make_room(uid, name, capacity):
    if not name.strip():
        raise UserError("방 이름을 입력해 주세요.")
    for _ in range(8):
        try:
            with connect() as c:
                r = c.execute("""INSERT INTO fc_rooms(name,owner_id,code,capacity)
                    VALUES (%s,%s,%s,%s) RETURNING id""",
                    (name.strip(), uid, code7(), int(capacity))).fetchone()
                c.execute("INSERT INTO fc_members(room_id,user_id) VALUES (%s,%s)",
                          (r['id'], uid))
                return r['id']
        except psycopg.errors.UniqueViolation:
            continue
    raise UserError("초대코드를 생성하지 못했습니다. 다시 시도해 주세요.")


def join_room(uid, code):
    code = code.strip().upper()
    if len(code) != 7 or any(ch not in string.ascii_uppercase + string.digits for ch in code):
        raise UserError("영문 대문자와 숫자로 된 7자리 코드를 확인해 주세요.")
    with connect() as c:
        r = c.execute("SELECT * FROM fc_rooms WHERE code=%s FOR UPDATE", (code,)).fetchone()
        if not r:
            raise UserError("유효하지 않거나 변경된 초대코드입니다.")
        if c.execute("SELECT 1 FROM fc_members WHERE room_id=%s AND user_id=%s",
                     (r['id'], uid)).fetchone():
            return r['id']
        n = c.execute("SELECT count(*) AS n FROM fc_members WHERE room_id=%s",
                      (r['id'],)).fetchone()['n']
        if n >= r['capacity']:
            raise UserError("방 정원이 가득 찼습니다. 방장에게 문의해 주세요.")
        c.execute("INSERT INTO fc_members(room_id,user_id) VALUES (%s,%s)", (r['id'], uid))
        return r['id']


def save_event(uid, rid, eid, title, start, end, location, notes):
    if not title.strip():
        raise UserError("일정 제목을 입력해 주세요.")
    if end <= start:
        raise UserError("종료 시간은 시작 시간보다 늦어야 합니다.")
    with connect() as c:
        room = room_for(c, rid, uid, lock=True)
        if eid is None:
            c.execute("""INSERT INTO fc_events
                (room_id,author_id,title,starts_at,ends_at,location,notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (rid, uid, title.strip(), start, end, location, notes))
        else:
            old = c.execute("SELECT * FROM fc_events WHERE id=%s AND room_id=%s",
                            (eid, rid)).fetchone()
            if not old or uid not in (old['author_id'], room['owner_id']):
                raise UserError("작성자 또는 방장만 수정할 수 있습니다.")
            c.execute("""UPDATE fc_events SET title=%s,starts_at=%s,ends_at=%s,
                         location=%s,notes=%s WHERE id=%s""",
                      (title.strip(), start, end, location, notes, eid))


def mutate(uid, rid, action, value=None):
    with connect() as c:
        r = room_for(c, rid, uid, lock=True)
        if action == 'delete_event':
            e = c.execute("SELECT * FROM fc_events WHERE id=%s AND room_id=%s",
                          (value, rid)).fetchone()
            if not e or uid not in (e['author_id'], r['owner_id']):
                raise UserError("작성자 또는 방장만 삭제할 수 있습니다.")
            c.execute("DELETE FROM fc_events WHERE id=%s", (value,))
        elif action == 'leave':
            if uid == r['owner_id']:
                raise UserError("방장은 탈퇴 대신 방 삭제를 이용해 주세요.")
            c.execute("DELETE FROM fc_members WHERE room_id=%s AND user_id=%s", (rid, uid))
        else:
            if r['owner_id'] != uid:
                raise UserError("방장만 관리할 수 있습니다.")
            if action == 'capacity':
                n = c.execute("SELECT count(*) AS n FROM fc_members WHERE room_id=%s",
                              (rid,)).fetchone()['n']
                if not n <= int(value) <= 100:
                    raise UserError("정원은 현재 인원 이상, 100명 이하여야 합니다.")
                c.execute("UPDATE fc_rooms SET capacity=%s WHERE id=%s", (int(value), rid))
            elif action == 'rotate':
                c.execute("UPDATE fc_rooms SET code=%s WHERE id=%s", (code7(), rid))
            elif action == 'delete_room':
                c.execute("DELETE FROM fc_rooms WHERE id=%s", (rid,))


def attempt(fn, *args):
    try:
        return True, fn(*args)
    except UserError as exc:
        st.warning(str(exc))
    except Exception:
        LOG.exception("Calendar operation failed")
        st.error("처리하지 못했습니다. 연결 상태를 확인한 뒤 다시 시도해 주세요.")
    return False, None


def event_form(uid, rid, day, event=None):
    eid = event['id'] if event else None
    start = event['starts_at'].astimezone(TZ) if event else datetime.combine(day, time(9), TZ)
    end = event['ends_at'].astimezone(TZ) if event else start + timedelta(hours=1)
    with st.form(f"event_{rid}_{eid or 'new'}"):
        title = st.text_input("제목", value=event['title'] if event else '', max_chars=120)
        a, b = st.columns(2)
        sd = a.date_input("시작 날짜", value=start.date())
        ed = b.date_input("종료 날짜", value=end.date())
        stime = a.time_input("시작 시간", value=start.time().replace(tzinfo=None), step=300)
        etime = b.time_input("종료 시간", value=end.time().replace(tzinfo=None), step=300)
        loc = st.text_input("장소", value=event['location'] if event else '', max_chars=300)
        notes = st.text_area("세부사항 · 준비물 · 함께할 가족",
                             value=event['notes'] if event else '', max_chars=5000)
        if st.form_submit_button("변경 저장" if event else "일정 추가", type="primary"):
            ok, _ = attempt(save_event, uid, rid, eid, title,
                            datetime.combine(sd, stime, TZ), datetime.combine(ed, etime, TZ),
                            loc, notes)
            if ok:
                st.rerun()


@st.fragment(run_every="1s")
def clock():
    st.caption("🕒 서울 기준 현재 시간 · " + datetime.now(TZ).strftime('%Y.%m.%d %H:%M:%S'))


@st.fragment(run_every="10s")
def calendar_view(uid, rid):
    with connect() as c:
        room_for(c, rid, uid)
        month = st.session_state['month']
        selected = st.session_state['day']
        first = month.replace(day=1)
        following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        lo = datetime.combine(min(first, selected), time.min, TZ)
        hi = datetime.combine(max(following, selected + timedelta(days=1)), time.min, TZ)
        events = c.execute("""SELECT e.*,u.name AS author_name FROM fc_events e
            JOIN fc_users u ON u.id=e.author_id
            WHERE e.room_id=%s AND e.starts_at<%s AND e.ends_at>%s
            ORDER BY e.starts_at,e.id""", (rid, hi, lo)).fetchall()
        r = room_for(c, rid, uid)
    st.html('<div class="month-kicker">OUR DAYS · SHARED CALENDAR</div>')
    left, middle, right = st.columns([1, 5, 1])
    if left.button("‹", help="지난달", key='prev_month', use_container_width=True):
        st.session_state['month'] = (first - timedelta(days=1)).replace(day=1)
        st.rerun()
    middle.subheader(f"{first.year}년 {first.month}월")
    if right.button("›", help="다음 달", key='next_month', use_container_width=True):
        st.session_state['month'] = following
        st.rerun()
    with st.container(key='month_grid'):
        for col, label in zip(st.columns(7), ['일', '월', '화', '수', '목', '금', '토']):
            col.caption(label)
        for week in calendar.Calendar(firstweekday=6).monthdatescalendar(first.year, first.month):
            for col, d in zip(st.columns(7), week):
                with col:
                    with st.container(key=f'daycell_{d.isoformat()}'):
                        ds = datetime.combine(d, time.min, TZ)
                        de = ds + timedelta(days=1)
                        inside = d.month == first.month
                        daily = [e for e in events if e['starts_at'] < de and e['ends_at'] > ds] if inside else []
                        tone = 'sun' if d.weekday() == 6 else 'sat' if d.weekday() == 5 else 'week'
                        is_today = d == datetime.now(TZ).date()
                        if st.button(f"{d.day}" + (' •' if is_today else ''),
                                     key=f'date_{tone}_{d.isoformat()}', disabled=not inside,
                                     use_container_width=True,
                                     help='오늘' if is_today else d.isoformat(),
                                     type='primary' if d == selected else 'secondary'):
                            st.session_state['day'] = d
                            st.session_state.pop('detail_id', None)
                            st.rerun()
                        for e in daily[:2]:
                            color = int(hashlib.sha256(e['author_id'].encode()).hexdigest()[:8], 16) % 6
                            tooltip = e['starts_at'].astimezone(TZ).strftime('%H:%M') + ' · ' + e['title']
                            if st.button(e['title'], key=f"chip{color}_{d.isoformat()}_{e['id']}",
                                         help=tooltip, use_container_width=True):
                                st.session_state['day'] = d
                                st.session_state['detail_id'] = e['id']
                                st.rerun()
                        if len(daily) > 2:
                            if st.button(f"+{len(daily)-2}개", key=f'more_{d.isoformat()}',
                                         use_container_width=True):
                                st.session_state['day'] = d
                                st.session_state.pop('detail_id', None)
                                st.rerun()
    st.caption("색상은 작성자별로 구분돼요. 날짜를 누르면 시간표, 일정 태그를 누르면 아래에 상세정보가 열립니다.")
    day_start = datetime.combine(selected, time.min, TZ)
    day_end = day_start + timedelta(days=1)
    todays = [e for e in events if e['starts_at'] < day_end and e['ends_at'] > day_start]
    st.subheader(f"{selected:%m월 %d일} · 시간별 일정")
    if not todays:
        st.info("등록된 일정이 없습니다. 아래에서 첫 일정을 추가해 보세요.")
    for e in todays:
        a, b = e['starts_at'].astimezone(TZ), e['ends_at'].astimezone(TZ)
        span = f"{a:%H:%M}–{b:%H:%M}" if a.date() == b.date() else f"{a:%m/%d %H:%M}–{b:%m/%d %H:%M}"
        if st.button(f"{span}  ·  {e['title']}", key=f"open_{e['id']}", use_container_width=True):
            st.session_state['detail_id'] = e['id']
            st.rerun()
    chosen = next((e for e in todays if e['id'] == st.session_state.get('detail_id')), None)
    if chosen:
        with st.container(border=True):
            st.subheader(chosen['title'])
            st.write("시작:", chosen['starts_at'].astimezone(TZ).strftime('%Y-%m-%d %H:%M'))
            st.write("종료:", chosen['ends_at'].astimezone(TZ).strftime('%Y-%m-%d %H:%M'))
            st.write("장소:", chosen['location'] or '미입력')
            st.write("작성자:", chosen['author_name'])
            st.text(chosen['notes'] or '세부사항이 없습니다.')
            if uid in (chosen['author_id'], r['owner_id']):
                with st.expander("✏️ 일정 수정"):
                    event_form(uid, rid, selected, chosen)
                confirm = st.checkbox("이 일정을 삭제합니다", key=f"confirm_e_{chosen['id']}")
                if st.button("일정 삭제", disabled=not confirm, key=f"delete_e_{chosen['id']}"):
                    ok, _ = attempt(mutate, uid, rid, 'delete_event', chosen['id'])
                    if ok:
                        st.session_state.pop('detail_id', None)
                        st.rerun()
    with st.expander("＋ 선택한 날짜에 일정 추가", expanded=not todays):
        event_form(uid, rid, selected)
    st.caption("다른 가족의 변경사항은 최대 약 10초 간격으로 반영됩니다.")



def apply_design():
    # Only trusted CSS is injected; all user-provided text remains native widget text.
    st.html("""<style>
    .block-container {max-width:1080px;padding-top:2rem;padding-bottom:3rem;}
    h1 {font-size:1.65rem!important;letter-spacing:-.055em;}
    h2,h3 {letter-spacing:-.035em;}
    .st-key-month_grid {border:1px solid color-mix(in srgb,var(--text-color) 12%,transparent);
      border-radius:20px;padding:12px 10px 6px;}
    .st-key-month_grid [data-testid="stHorizontalBlock"] {gap:4px!important;flex-wrap:nowrap!important;}
    .st-key-month_grid [data-testid="stColumn"] {min-width:0!important;flex:1 1 0!important;width:0!important;}
    .st-key-month_grid [data-testid="stVerticalBlock"] {gap:3px!important;}
    [class*="st-key-daycell_"] {min-height:120px;padding:3px 1px 7px;
      border-top:1px solid color-mix(in srgb,var(--text-color) 10%,transparent);}
    [class*="st-key-date_"] button {border:0!important;background:transparent!important;
      min-height:27px!important;padding:2px 4px!important;box-shadow:none!important;}
    [class*="st-key-date_"] button p {font-size:12px!important;font-weight:650;}
    [class*="st-key-date_sun_"] button {color:#d34f59!important;}
    [class*="st-key-date_sat_"] button {color:#4977d0!important;}
    [class*="st-key-date_"] button[kind="primary"] {background:#146b60!important;color:#fff!important;border-radius:9px!important;}
    [class*="st-key-date_"] button:disabled {opacity:.32;}
    [class*="st-key-chip"] button {min-height:23px!important;height:23px!important;
      border:0!important;border-radius:5px!important;padding:1px 5px!important;justify-content:flex-start!important;}
    [class*="st-key-chip"] button p {font-size:11px!important;line-height:18px!important;
      overflow:hidden!important;white-space:nowrap!important;text-overflow:ellipsis!important;display:block!important;}
    [class*="st-key-chip0_"] button {background:#d9eee7!important;color:#205848!important;}
    [class*="st-key-chip1_"] button {background:#f9e3d4!important;color:#7a4024!important;}
    [class*="st-key-chip2_"] button {background:#e7e1f5!important;color:#58417d!important;}
    [class*="st-key-chip3_"] button {background:#dceaf6!important;color:#295775!important;}
    [class*="st-key-chip4_"] button {background:#f6dfe7!important;color:#82354f!important;}
    [class*="st-key-chip5_"] button {background:#edeacb!important;color:#5e571f!important;}
    [class*="st-key-more_"] button {border:0!important;background:transparent!important;
      padding:0!important;min-height:18px!important;}
    [class*="st-key-more_"] button p {font-size:10px!important;opacity:.7;}
    .month-kicker {font-size:10px;font-weight:700;letter-spacing:.18em;opacity:.55;margin:8px 0;}
    .st-key-month_grid [data-testid="stCaptionContainer"] p {text-align:center;font-size:11px;}
    @media(max-width:600px) {
      .block-container {padding-left:.65rem;padding-right:.65rem;padding-top:1rem;}
      .st-key-month_grid {padding:8px 4px 4px;border-radius:14px;}
      .st-key-month_grid [data-testid="stHorizontalBlock"] {gap:2px!important;}
      [class*="st-key-daycell_"] {min-height:101px;}
      [class*="st-key-chip"] button {padding:1px 2px!important;height:21px!important;min-height:21px!important;}
      [class*="st-key-chip"] button p {font-size:9px!important;}
      [class*="st-key-date_"] button {padding:0!important;}
    }
    </style>""")


def main():
    # Streamlit의 네이티브 테마 전환을 사용해 모든 위젯/달력을 일관되게 적용합니다.
    apply_design()
    st.title("우리 사이 · 캘린더")
    st.caption("함께 약속하고, 시간을 나누는 가족의 달력")
    st.sidebar.header("화면 설정")
    st.sidebar.caption("밝게 / 어둡게: 오른쪽 위 ⋮ → Settings → Theme에서 Light 또는 Dark를 선택하세요. 기기별로 적용됩니다.")
    clock()
    try:
        configured = bool(st.secrets.get('DATABASE_URL') and st.secrets.get('APP_URL'))
    except Exception:
        configured = False
    if not configured:
        st.subheader("환영합니다! 배포자가 최초 연결을 완료해야 합니다.")
        st.info("Google 설정은 필요 없습니다. 앱 Settings → Secrets에 APP_URL과 DATABASE_URL 두 값만 입력하세요. 상세 안내는 app.py 맨 위에 있습니다.")
        st.stop()
    invite = str(st.query_params.get('invite', '')).strip().upper()
    if invite:
        st.session_state['pending_invite'] = invite
    initialize()
    if (st.session_state.get('local_uid') and
            systime.time() - st.session_state.get('login_at', 0) >= 12 * 3600):
        st.session_state.pop('local_uid', None)
    uid = st.session_state.get('local_uid')
    if not uid:
        auth_screen()
        st.stop()
    with connect() as c:
        user = c.execute("SELECT name FROM fc_users WHERE id=%s", (uid,)).fetchone()
        if not user:
            st.session_state.clear()
            st.rerun()
        name = user['name']
        rooms = c.execute("""SELECT r.* FROM fc_rooms r JOIN fc_members m ON m.room_id=r.id
                              WHERE m.user_id=%s ORDER BY r.created_at DESC""", (uid,)).fetchall()
    st.sidebar.write(f"{name} 님")
    if st.sidebar.button("로그아웃"):
        st.session_state.clear()
        st.rerun()
    with st.sidebar.expander("＋ 새 캘린더 방 만들기"):
        with st.form('create_room'):
            rn = st.text_input("방 이름", max_chars=80, placeholder="우리 가족")
            cap = st.number_input("최대 인원 (방장 포함)", 1, 100, 8)
            if st.form_submit_button("방 만들기"):
                ok, rid = attempt(make_room, uid, rn, cap)
                if ok:
                    st.session_state['active_room'] = rid
                    st.rerun()
    with st.sidebar.expander("초대코드로 입장", expanded=bool(st.session_state.get('pending_invite'))):
        with st.form('join_room'):
            code = st.text_input("7자리 초대코드", value=st.session_state.get('pending_invite', ''), max_chars=7)
            if st.form_submit_button("입장"):
                last = st.session_state.get('last_join_attempt', 0)
                if systime.monotonic() - last < 3:
                    st.warning("잠시 후 다시 입장해 주세요.")
                    st.stop()
                st.session_state['last_join_attempt'] = systime.monotonic()
                ok, rid = attempt(join_room, uid, code)
                if ok:
                    st.session_state['active_room'] = rid
                    st.session_state.pop('pending_invite', None)
                    st.query_params.clear()
                    st.rerun()
    if not rooms:
        st.info("왼쪽에서 방을 만들거나 초대코드를 입력하세요. 모바일에서는 왼쪽 위 > 버튼으로 메뉴를 펼치세요.")
        return
    ids = [r['id'] for r in rooms]
    if st.session_state.get('active_room') not in ids:
        st.session_state['active_room'] = ids[0]
    labels = {r['id']: r['name'] for r in rooms}
    rid = st.pills("공유 캘린더", ids, key='active_room', format_func=lambda x: labels[x]) or ids[0]
    room = next(r for r in rooms if r['id'] == rid)
    st.caption('현재 캘린더 · ' + room['name'])
    with connect() as c:
        room_for(c, rid, uid)
        members = c.execute("""SELECT u.id,u.name FROM fc_members m JOIN fc_users u
            ON u.id=m.user_id WHERE m.room_id=%s ORDER BY m.joined_at""", (rid,)).fetchall()
    st.caption(f"참여 {len(members)} / {room['capacity']}명 · 방장 포함")
    with st.expander("가족 초대 · QR · 참여자 보기"):
        st.write('참여 가족: ' + ', '.join(m['name'] for m in members))
        st.code(room['code'], language=None)
        base = str(st.secrets['APP_URL']).rstrip('/')
        parsed = urlsplit(base)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.query or parsed.fragment:
            st.warning("APP_URL에는 쿼리 없는 앱 기본 주소를 설정해 주세요.")
        else:
            link = base + '/?' + urlencode({'invite': room['code']})
            buf = io.BytesIO()
            qrcode.make(link).save(buf, format='PNG')
            st.image(buf.getvalue(), width=220)
            st.link_button("초대 링크 열기", link)
            st.code(link, language=None)
            st.download_button("QR 이미지 다운로드", buf.getvalue(), file_name='family-invite.png', mime='image/png')
        st.caption("QR은 로그인 후 입장을 요청하는 링크입니다. 자동 가입은 아니며 정원이 적용됩니다.")
    today = datetime.now(TZ).date()
    st.session_state.setdefault('month', today.replace(day=1))
    st.session_state.setdefault('day', today)
    if st.button("오늘로 이동"):
        st.session_state['month'] = today.replace(day=1)
        st.session_state['day'] = today
        st.session_state.pop('detail_id', None)
        st.rerun()
    calendar_view(uid, rid)
    with st.expander("방 관리"):
        if room['owner_id'] == uid:
            cap = st.number_input("최대 수용 인원 변경", 1, 100, room['capacity'], key=f'cap_{rid}')
            if st.button("정원 변경"):
                ok, _ = attempt(mutate, uid, rid, 'capacity', cap)
                if ok:
                    st.rerun()
            st.caption("초대코드를 변경하면 기존 QR과 초대 링크는 더 이상 사용할 수 없습니다.")
            if st.button("초대코드 재발급"):
                ok, _ = attempt(mutate, uid, rid, 'rotate')
                if ok:
                    st.rerun()
            confirm = st.checkbox("방과 모든 일정이 영구 삭제됨을 확인합니다", key=f'delete_room_{rid}')
            if st.button("방 영구 삭제", disabled=not confirm):
                ok, _ = attempt(mutate, uid, rid, 'delete_room')
                if ok:
                    st.rerun()
        else:
            confirm = st.checkbox("이 방에서 나갑니다. 기존 작성 일정은 남습니다.")
            if st.button("방 나가기", disabled=not confirm):
                ok, _ = attempt(mutate, uid, rid, 'leave')
                if ok:
                    st.rerun()


if __name__ == '__main__':
    try:
        main()
    except UserError as exc:
        st.warning(str(exc))
        if st.button("방 목록 새로고침"):
            st.rerun()
    except Exception:
        LOG.exception('Application failed')
        st.error("앱에 연결하지 못했습니다. 배포자는 APP_URL과 DATABASE_URL 및 DB 연결을 확인해 주세요. 비밀번호나 연결 문자열을 공개하지 마세요.")
