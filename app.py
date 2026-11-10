"""말로 쓰는 영농일지
실행: streamlit run app.py
개인용 시제품: 기록은 브라우저 연결 세션에만 보관됩니다. CSV 백업 필수.
음성 인식은 외부 Google 음성 인식 서비스를 사용하며 별도 API 키가 필요 없습니다.
작업 항목 정리는 생성형 AI가 아닌 규칙 기반입니다.
"""
from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

FIELDS = ["id", "날짜", "밭·장소", "작물", "작업", "자재·사용량", "작업시간(분)", "메모", "원문"]
TASKS = {
    "비료": ["비료", "시비", "웃거름", "밑거름", "퇴비"],
    "물주기": ["물주", "물 주", "관수", "물 줬", "물 줌"],
    "방제": ["방제", "농약", "살포", "약 쳤", "약을 쳤"],
    "수확": ["수확", "수확했", "땄", "캤"],
    "심기": ["파종", "정식", "심었", "모종", "모내기"],
    "제초": ["제초", "풀 뽑", "풀을 뽑", "예초", "잡초"],
    "가지치기": ["전정", "가지치기", "가지 치기"],
    "출하": ["출하", "납품"],
}
CROPS = ["고추", "벼", "토마토", "감자", "고구마", "배추", "무", "상추", "딸기", "사과", "배", "포도", "오이", "호박", "마늘", "양파", "콩", "옥수수", "참외", "수박", "복숭아", "감귤"]
KOREAN_NUMBERS = {"한": 1, "하나": 1, "두": 2, "둘": 2, "세": 3, "셋": 3, "네": 4, "넷": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10}
NUM = r"(?:\d+(?:\.\d+)?|" + "|".join(sorted(KOREAN_NUMBERS, key=len, reverse=True)) + r")"


def today_kst():
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


def number(token):
    return float(KOREAN_NUMBERS[token]) if token in KOREAN_NUMBERS else float(token)


def parse_note(text, today=None):
    """확실히 표현된 항목만 초안으로 추출. 자재 무게/면적/날씨는 추정하지 않음."""
    today = today or today_kst()
    day = today
    if "그저께" in text or "그제" in text:
        day -= timedelta(days=2)
    elif "어제" in text:
        day -= timedelta(days=1)
    explicit = re.search(r"(\d{4})[년./-]\s*(\d{1,2})[월./-]\s*(\d{1,2})일?", text)
    warning = ""
    if explicit:
        try:
            day = date(*map(int, explicit.groups()))
        except ValueError:
            warning = "날짜 표현을 해석하지 못했습니다. 날짜를 직접 확인하세요."
    elif re.search(r"지난|저번|내일|모레|\d+\s*월|\d+\s*일\s*전", text):
        warning = "상대 날짜 또는 월·일 표현은 자동 변환하지 않았습니다. 날짜를 직접 지정하세요."
    field = re.search(r"\d+\s*번\s*(?:밭|논|하우스)|\d+\s*동\s*하우스|(?:앞|뒤|윗|아랫)\s*(?:밭|논)", text)
    crops = [c for c in CROPS if re.search(r"(?<![가-힣])" + re.escape(c) + r"(?=$|[^가-힣]|밭|논|을|를|에|은|는|이|가|도|와|과)", text)]
    tasks = [label for label, words in TASKS.items() if any(word in text for word in words)]
    hour = re.search(r"(" + NUM + r")\s*시간(?:\s*(반))?", text)
    minute = re.search(r"(" + NUM + r")\s*분(?![가-힣])", text)
    duration = (number(hour.group(1)) * 60 + (30 if hour.group(2) else 0)) if hour else 0
    if minute:
        duration += number(minute.group(1))
    quantities = re.findall(r"" + NUM + r"\s*(?:포대|봉지|상자|박스|리터|킬로그램|kg|㎏|ml|mL|L|통|개|톤)(?![a-zA-Z])", text)
    materials = [word for word in ["비료", "퇴비", "농약", "모종", "씨앗"] if word in text]
    material_note = " / ".join(materials + quantities)
    return {"날짜": day, "밭·장소": field.group() if field else "", "작물": ", ".join(crops),
            "작업": ", ".join(tasks), "자재·사용량": material_note,
            "작업시간(분)": int(round(duration)), "메모": "", "원문": text, "경고": warning}


def csv_safe(value):
    text = str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def csv_unsafe(value):
    # 이 앱의 수식 주입 방지 접두사를 복원합니다.
    return value[1:] if value.startswith("'") and value[1:].lstrip().startswith(("=", "+", "-", "@")) else value


def export_csv(records):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=FIELDS)
    writer.writeheader()
    for record in records:
        writer.writerow({key: csv_safe(record.get(key, "")) for key in FIELDS})
    return out.getvalue().encode("utf-8-sig")


def import_csv(data):
    if len(data) > 5_000_000:
        raise ValueError("5MB 이하의 백업 파일을 선택하세요.")
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    if reader.fieldnames != FIELDS:
        raise ValueError("이 앱에서 내려받은 CSV 백업 파일을 선택하세요. 열 이름과 순서가 일치해야 합니다.")
    result = []
    ids = set()
    for index, row in enumerate(reader, 2):
        if index > 10001:
            raise ValueError("한 번에 최대 10,000개 기록을 불러올 수 있습니다.")
        if None in row or any(v is None for v in row.values()):
            raise ValueError(f"{index}행의 열 개수가 맞지 않습니다.")
        row = {k: csv_unsafe(v) for k, v in row.items()}
        if any(len(v) > 10000 for v in row.values()):
            raise ValueError(f"{index}행의 텍스트가 너무 깁니다.")
        try:
            row["날짜"] = date.fromisoformat(row["날짜"]).isoformat()
            minutes = int(row["작업시간(분)"])
            if not 0 <= minutes <= 1440:
                raise ValueError()
        except ValueError:
            raise ValueError(f"{index}행의 날짜 또는 작업시간이 올바르지 않습니다.")
        if not row["id"] or row["id"] in ids or not row["작업"].strip():
            raise ValueError(f"{index}행의 ID가 없거나 중복되었거나 작업 내용이 비어 있습니다.")
        row["작업시간(분)"] = minutes
        ids.add(row["id"])
        result.append(row)
    return result


def search_records(records, query):
    """소규모 자연어 표현을 키워드로 바꾸는 검색. 생성형 질의응답 아님."""
    q = query.strip()
    if not q:
        return sorted(records, key=lambda r: r["날짜"], reverse=True)
    terms = []
    for label, words in TASKS.items():
        if label in q or any(word in q for word in words):
            terms.append(label)
            for word in sorted(set(words + [label]), key=len, reverse=True):
                q = q.replace(word, " ")
    q = re.sub(r"지난번|마지막으로|마지막|최근|언제|알려줘|찾아줘|기록|했지|했어|줬지|줬어|뿌렸지|뿌렸어|작업", " ", q)
    for token in q.split():
        token = re.sub(r"(?:에서|에게|에는|은|는|이|가|을|를|에|도|의)[?.!]*$", "", token).strip(" ?.!")
        if token:
            terms.append(token)
    if not terms:
        return sorted(records, key=lambda r: r["날짜"], reverse=True)
    def matches(row):
        haystack = re.sub(r"\s+", "", " ".join(str(row.get(k, "")) for k in FIELDS if k != "id")).lower()
        return all(re.sub(r"\s+", "", term).lower() in haystack for term in terms)
    return sorted([r for r in records if matches(r)], key=lambda r: r["날짜"], reverse=True)


def main():
    st.set_page_config(page_title="말로 쓰는 영농일지", page_icon="🌱", layout="centered")
    st.markdown("""<style>
    .stApp {background-color: #f7f9f5;}
    h1, h2, h3 {color: #254b32;}
    div.stButton > button {min-height: 3rem;}
    [data-testid='stTextArea'] textarea {font-size: 18px;}
    </style>""", unsafe_allow_html=True)
    state = st.session_state
    for key, default in {"records": [], "raw_note": "", "draft": None, "draft_id": 0}.items():
        if key not in state:
            state[key] = default
    st.title("🌱 말로 쓰는 영농일지")
    st.write("오늘 한 일을 말하고, 확인한 뒤 기록하세요.")
    st.warning("기록은 현재 접속 세션에만 저장됩니다. 새로고침·접속 종료·서버 재시작 시 사라질 수 있으니, 사용 후 아래에서 CSV 백업을 내려받으세요. 다른 접속자의 기록과 공유하지 않습니다.")
    st.caption("API 키 없이 사용 · 음성 인식은 외부 AI 서비스 · 항목 정리와 검색은 규칙 기반")
    if state.pop("saved_message", None):
        st.success("기록을 저장했습니다. 사용을 마치기 전에 CSV 백업을 내려받으세요.")

    write_tab, list_tab, backup_tab = st.tabs(["기록하기", "지난 기록 찾기", "백업·불러오기"])
    with write_tab:
        st.subheader("1. 오늘 한 일 입력")
        st.caption("예: 오늘 2번 밭에 고추 비료 두 포대 줬고, 세 시간 일했어.")
        consent = st.checkbox("음성 인식을 위해 음성이 외부 Google 서비스로 전송되는 데 동의합니다.", key="voice_consent")
        if consent:
            st.caption("마이크 권한과 인터넷 연결이 필요합니다. Chrome 또는 Edge를 권장합니다. 주민번호 등 민감한 정보는 말하지 마세요.")
            try:
                from streamlit_mic_recorder import speech_to_text
                text = speech_to_text(language="ko-KR", start_prompt="🎤 말하기 시작", stop_prompt="⏹ 말하기 끝", just_once=True, use_container_width=True, key="farm_voice")
                if text:
                    state.raw_note = text
                    state.draft = None
                    st.success("음성을 글로 바꿨습니다. 아래 내용을 확인하세요.")
            except Exception:
                st.warning("음성 인식에 연결하지 못했습니다. 마이크 권한·인터넷 연결을 확인하거나 아래에 직접 입력하세요.")
        st.caption("음성 버튼이 작동하지 않으면 휴대폰 키보드의 마이크로 받아쓰거나 직접 입력해도 됩니다. 새 음성 인식 결과는 아래 입력 내용을 교체합니다.")
        raw = st.text_area("작업 내용", key="raw_note", height=150, max_chars=5000, placeholder="언제, 어느 밭에서, 무슨 일을 얼마나 했는지 말씀해 주세요.")
        if st.button("내용 정리하기", type="primary", use_container_width=True):
            if not raw.strip():
                st.error("먼저 작업 내용을 입력하세요.")
            else:
                state.draft = parse_note(raw.strip())
                state.draft_id += 1
        if state.draft:
            draft = state.draft
            st.subheader("2. 확인하고 저장")
            st.info("자동 정리는 초안입니다. 음성 인식 오류, 날짜, 작업, 수량을 확인해 주세요. '두 포대'를 kg으로 임의 환산하지 않습니다. 여러 작업의 수량은 직접 구분해 주세요.")
            if draft.get("경고"):
                st.warning(draft["경고"])
            if raw.strip() != draft["원문"]:
                st.warning("원문이 바뀌었습니다. 수정한 내용을 반영하려면 '내용 정리하기'를 다시 누르세요.")
            with st.form(f"draft_form_{state.draft_id}"):
                day = st.date_input("작업 날짜", value=draft["날짜"])
                field = st.text_input("밭·장소", value=draft["밭·장소"], max_chars=100)
                crop = st.text_input("작물", value=draft["작물"], max_chars=100)
                task = st.text_input("작업 내용 (필수)", value=draft["작업"], max_chars=500)
                material = st.text_input("자재·사용량", value=draft["자재·사용량"], max_chars=500, placeholder="예: 복합비료 두 포대. 포대당 무게는 아는 경우만 기록")
                minutes = st.number_input("작업시간 (분 / 모르면 0)", min_value=0, max_value=1440, value=min(1440, draft["작업시간(분)"]), step=10)
                memo = st.text_area("추가 메모", value=draft["메모"], max_chars=3000)
                if st.form_submit_button("영농일지 저장", type="primary", use_container_width=True):
                    if raw.strip() != draft["원문"]:
                        st.error("원문이 변경되었습니다. 먼저 '내용 정리하기'를 다시 눌러주세요.")
                    elif not task.strip():
                        st.error("작업 내용을 입력하세요.")
                    else:
                        state.records.append({"id": str(uuid.uuid4()), "날짜": day.isoformat(), "밭·장소": field.strip(), "작물": crop.strip(), "작업": task.strip(), "자재·사용량": material.strip(), "작업시간(분)": int(minutes), "메모": memo.strip(), "원문": draft["원문"]})
                        state.draft = None
                        state.saved_message = True
                        # raw_note 위젯은 이번 실행에서 이미 생성되어 있으므로 여기서 변경하지 않습니다.
                        st.rerun()

    with list_tab:
        st.subheader("지난 기록 찾기")
        st.caption("예: 비료 / 2번 밭 / 지난번 비료 언제 줬지. 복잡한 질문 대신 짧은 키워드를 써 주세요.")
        query = st.text_input("검색어 또는 간단한 질문", max_chars=200)
        matches = search_records(state.records, query)
        if state.records:
            dates = [date.fromisoformat(r["날짜"]) for r in state.records]
            c1, c2 = st.columns(2)
            with c1:
                start = st.date_input("시작일", min(dates), key="filter_start")
            with c2:
                end = st.date_input("종료일", max(max(dates), today_kst()), key="filter_end")
            if start > end:
                st.error("시작일이 종료일보다 늦습니다.")
                matches = []
            else:
                matches = [r for r in matches if start.isoformat() <= r["날짜"] <= end.isoformat()]
            st.write(f"검색 결과 **{len(matches)}건**")
            if matches:
                latest = matches[0]
                st.info(f"검색 조건에 맞는 가장 최근 기록: {latest['날짜']} · {latest['밭·장소'] or '장소 미입력'} · {latest['작업']}")
                st.caption("같은 날짜에 여러 기록이 있으면 실제 작업 순서는 구분하지 않습니다. 전체 기록은 백업 파일에서 확인할 수 있습니다.")
            for r in matches[:100]:
                with st.expander(f"{r['날짜']} | {r['밭·장소'] or '장소 미입력'} | {r['작업']}"):
                    st.write({key: r[key] for key in FIELDS if key not in ("id", "원문")})
                    st.text("입력 원문: " + r["원문"])
                    if st.checkbox("이 기록을 삭제하겠습니다", key=f"confirm_{r['id']}"):
                        if st.button("기록 삭제", key=f"delete_{r['id']}"):
                            state.records = [x for x in state.records if x["id"] != r["id"]]
                            st.rerun()
            if len(matches) > 100:
                st.caption("최근 100건만 표시했습니다. 검색어와 기간으로 범위를 좁혀 주세요.")
            if not matches:
                st.caption("일치하는 기록이 없습니다. 검색어 또는 기간을 바꿔보세요.")
        else:
            st.info("아직 기록이 없습니다. 첫 기록을 저장하거나 CSV 백업을 불러오세요.")

    with backup_tab:
        st.subheader("CSV 백업")
        st.write(f"현재 접속 세션에 {len(state.records)}건이 있습니다.")
        st.download_button("전체 기록 CSV 내려받기", data=export_csv(state.records), file_name=f"영농일지_{today_kst().isoformat()}.csv", mime="text/csv", disabled=not state.records, use_container_width=True)
        st.caption("파일은 휴대폰이나 컴퓨터의 다운로드 폴더에 저장됩니다. 기록이 담긴 CSV를 공개 GitHub 저장소에 올리지 마세요.")
        uploaded = st.file_uploader("이 앱에서 내려받은 CSV 불러오기", type=["csv"])
        if uploaded is not None and st.button("백업을 현재 기록에 합치기", use_container_width=True):
            try:
                imported = import_csv(uploaded.getvalue())
                existing = {r["id"] for r in state.records}
                additions = [r for r in imported if r["id"] not in existing]
                state.records.extend(additions)
                # 새 기록의 기간까지 검색 범위에 반영하도록 다음 실행에 필터를 초기화합니다.
                state["reset_filters"] = True
                state["import_notice"] = f"{len(additions)}건을 추가했습니다. 같은 ID의 기존 기록 {len(imported) - len(additions)}건은 덮어쓰지 않았습니다."
                st.rerun()
            except (ValueError, UnicodeDecodeError, csv.Error) as exc:
                st.error(f"불러오기에 실패했습니다: {exc}")
        if state.get("import_notice"):
            st.success(state["import_notice"])
        st.divider()
        st.caption("이 앱은 로그인·영구 데이터베이스 없는 개인용 시제품입니다. 장기 운영에는 사용자 인증과 사용자별 영구 저장소를 추가해야 합니다. 원본 음성을 앱의 파일 저장소에 따로 보관하지 않지만, 외부 음성 서비스의 데이터 처리 정책은 별도로 적용됩니다.")
        with st.expander("실행·배포 안내"):
            st.markdown("1. `app.py`와 `requirements.txt`를 같은 GitHub 저장소에 올립니다.\n2. Streamlit Community Cloud에서 해당 저장소를 선택합니다.\n3. 실행 파일을 `app.py`로 지정하고 배포합니다.\n4. 배포된 HTTPS 주소에서 마이크 권한을 허용합니다.\n\n로컬 실행: `pip install -r requirements.txt` 후 `streamlit run app.py`\n\nPython 3.11 또는 3.12를 권장합니다. 무료 음성 서비스는 브라우저·네트워크·서비스 제한에 따라 작동하지 않을 수 있습니다.")


if __name__ == "__main__":
    # 위젯이 생성되기 전에 검색 날짜 상태만 초기화합니다.
    if st.session_state.pop("reset_filters", False):
        for name in ("filter_start", "filter_end"):
            st.session_state.pop(name, None)
    main()
