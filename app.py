"""우리 가족 캘린더 — Streamlit + Google OIDC + PostgreSQL

배포 (저장소에 app.py, requirements.txt 두 파일만 올립니다):
1. Neon/Supabase 등에서 PostgreSQL DB를 생성하고 TLS 연결 문자열을 준비합니다.
   Supabase는 IPv4 호환 transaction pooler 연결 문자열을 권장합니다.
   이 앱 전용 DB/스키마와 최소 권한 DB 사용자를 사용하세요.
2. Google Cloud Console > Google Auth Platform에서 동의 화면과
   '웹 애플리케이션' OAuth 클라이언트를 생성합니다. 테스트 상태라면 가족의
   Google 계정을 테스트 사용자에 등록합니다. Google Calendar API는 불필요합니다.
3. Streamlit Community Cloud에서 GitHub 저장소, app.py를 선택해 배포합니다.
4. Google의 승인된 리디렉션 URI에 https://앱주소/oauth2callback을 등록합니다.
5. Streamlit 앱 Settings > Secrets에 다음 TOML을 입력합니다 (GitHub에 올리지 마세요).

APP_URL = "https://your-app.streamlit.app"
DATABASE_URL = "postgresql://USER:PASSWORD@HOST:5432/DB?sslmode=require"
[auth]
redirect_uri = "https://your-app.streamlit.app/oauth2callback"
cookie_secret = "별도로 생성한 충분히 긴 무작위 비밀문자열"
client_id = "Google 웹 OAuth 클라이언트 ID"
client_secret = "Google 웹 OAuth 클라이언트 비밀값"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"

로컬: pip install -r requirements.txt && streamlit run app.py
로컬 Secrets는 .streamlit/secrets.toml에 저장하고 Git에서 제외합니다.
로컬에서는 APP_URL=http://localhost:8501, redirect_uri 및 Google 리디렉션도
http://localhost:8501/oauth2callback로 변경합니다. 휴대폰 QR은 공개 HTTPS 주소 필요.
첫 Google 로그인 시 앱 회원 레코드가 자동 생성됩니다. 별도 비밀번호는 없습니다.
Google 로그인 후 초대 화면이 사라지면 QR을 한 번 더 열거나 코드를 입력하세요.
시간대는 Asia/Seoul 고정. 시계는 1초, 공유 일정은 10초 폴링(웹소켓 실시간 아님).
사용자 입력은 SQL 매개변수로 전달하고 HTML에 삽입하지 않습니다.
초대코드 소지자는 로그인 후 가입할 수 있으니 QR/코드를 공개하지 마세요.
"""
from __future__ import annotations

import calendar
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
    left, middle, right = st.columns([1, 4, 1])
    if left.button("◀", help="지난달", key='prev_month'):
        st.session_state['month'] = (first - timedelta(days=1)).replace(day=1)
        st.rerun()
    middle.subheader(first.strftime('%Y년 %m월'))
    if right.button("▶", help="다음 달", key='next_month'):
        st.session_state['month'] = following
        st.rerun()
    for col, label in zip(st.columns(7), ['월', '화', '수', '목', '금', '토', '일']):
        col.caption(label)
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month):
        for col, d in zip(st.columns(7), week):
            ds = datetime.combine(d, time.min, TZ)
            de = ds + timedelta(days=1)
            n = sum(e['starts_at'] < de and e['ends_at'] > ds for e in events)
            label = f"{d.day}" + (f" ·{n}" if n else '')
            if col.button(label, key=f'day_{d.isoformat()}',
                          disabled=d.month != first.month, use_container_width=True,
                          type='primary' if d == selected else 'secondary'):
                st.session_state['day'] = d
                st.session_state.pop('detail_id', None)
                st.rerun()
    st.caption("날짜 옆 숫자는 일정 개수입니다. 날짜를 누르면 아래 시간표가 바뀝니다.")
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


def main():
    # Streamlit의 네이티브 테마 전환을 사용해 모든 위젯/달력을 일관되게 적용합니다.
    st.title("📅 우리 가족 캘린더")
    st.caption("함께 약속하고, 시간을 나누는 가족의 달력")
    st.sidebar.header("화면 설정")
    st.sidebar.caption("밝게 / 어둡게: 오른쪽 위 ⋮ → Settings → Theme에서 Light 또는 Dark를 선택하세요. 기기별로 적용됩니다.")
    clock()
    try:
        configured = bool(st.secrets.get('DATABASE_URL') and st.secrets.get('APP_URL')
                          and st.secrets.get('auth'))
    except Exception:
        configured = False
    if not configured:
        st.info("배포 설정이 필요합니다. app.py 맨 위의 Google 로그인 / DB / Secrets 설정 안내를 따라 주세요.")
        st.stop()
    invite = str(st.query_params.get('invite', '')).strip().upper()
    if invite:
        st.session_state['pending_invite'] = invite
    if not st.user.is_logged_in:
        st.subheader("Google 계정으로 시작하기")
        st.write("처음 로그인하면 자동으로 가입됩니다. 비밀번호는 이 앱이 보관하지 않습니다.")
        st.caption("Google 이름·이메일과 작성한 일정이 저장되며, 같은 방의 가족은 이름과 일정을 볼 수 있습니다.")
        if invite:
            st.info(f"초대코드: {invite} · 로그인 후 초대 화면이 사라지면 QR을 다시 열거나 이 코드를 입력하세요.")
        if st.button("Google로 가입 / 로그인", type='primary'):
            st.login()
        st.stop()
    initialize()
    claims = st.user.to_dict()
    if claims.get('iss') not in ('https://accounts.google.com', 'accounts.google.com'):
        st.error("Google 로그인 설정을 확인해 주세요.")
        st.stop()
    if not claims.get('sub') or not claims.get('email_verified'):
        st.error("이메일 인증이 완료된 Google 계정이 필요합니다.")
        st.stop()
    uid = 'google:' + claims['sub']
    name = str(claims.get('name') or claims.get('email') or '가족')[:200]
    with connect() as c:
        c.execute("""INSERT INTO fc_users(id,name,email) VALUES (%s,%s,%s)
            ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,email=EXCLUDED.email""",
                  (uid, name, claims.get('email', '')))
        rooms = c.execute("""SELECT r.* FROM fc_rooms r JOIN fc_members m ON m.room_id=r.id
                              WHERE m.user_id=%s ORDER BY r.created_at DESC""", (uid,)).fetchall()
    st.sidebar.write(f"{name} 님")
    if st.sidebar.button("로그아웃"):
        st.session_state.clear()
        st.logout()
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
    rid = st.sidebar.selectbox("내 캘린더 방", ids, key='active_room', format_func=lambda x: labels[x])
    room = next(r for r in rooms if r['id'] == rid)
    st.header(room['name'])
    with connect() as c:
        room_for(c, rid, uid)
        members = c.execute("""SELECT u.id,u.name FROM fc_members m JOIN fc_users u
            ON u.id=m.user_id WHERE m.room_id=%s ORDER BY m.joined_at""", (rid,)).fetchall()
    st.caption(f"참여 {len(members)} / {room['capacity']}명 · 방장 포함")
    with st.expander("가족 · 초대코드 · QR"):
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
        st.error("앱에 연결하지 못했습니다. 배포자는 Secrets와 DB 연결을 확인해 주세요. 비밀번호나 연결 문자열을 공개하지 마세요.")
