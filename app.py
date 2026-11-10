"""농장노트 v2 — Streamlit 개인용 영농·현금 장부. 세션 저장, JSON 백업 필수."""
from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st
import pandas as pd
import json
import html
import math
from decimal import Decimal, ROUND_HALF_UP

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


# 모든 금액은 실제 입출금 기준 원 단위 정수로 저장합니다.
CATEGORIES = ['판매대금', '종묘·씨앗', '비료·퇴비', '방제비', '포장비', '운송비', '고용노동비', '임차료', '농기계·수리', '기타']
COLS = ['id','날짜','밭·장소','작물','작업','자재·사용량','작업시간(분)','거래구분','분류','금액(원)','메모','원문']

def won(value):
    return f'{int(value):,}원'


def cash_amount(text):
    """금액이 둘 이상이거나 단가 표현이 있으면 합계를 추정하지 않음."""
    hits = re.findall(r'(\d[\d,]*(?:\.\d+)?\s*억\s*)?(\d[\d,]*(?:\.\d+)?\s*만\s*)?(\d[\d,]*(?:\.\d+)?)?\s*원', text)
    values = []
    for eok, man, basic in hits:
        if not any((eok, man, basic)):
            continue
        def dec(s, unit):
            return Decimal(re.sub(r'[,\s'+unit+r']','',s)) if s else Decimal(0)
        v = dec(eok,'억')*100000000 + dec(man,'만')*10000 + dec(basic,'')
        values.append(int(v.quantize(Decimal('1'), rounding=ROUND_HALF_UP)))
    ambiguous = len(values) != 1 or bool(re.search(r'당|단가|씩|외상|미수|미지급|예정|견적',text))
    return (0 if ambiguous else values[0] if values else 0), ambiguous


def draft_from_text(text):
    d = parse_note(text)
    amount, unsure = cash_amount(text)
    income = any(x in text for x in ['팔았','팔고','받았','판매','입금','벌었'])
    expense = any(x in text for x in ['샀','사고','구매','썼','지출','지급','냈','주고'])
    kind = '확인 필요' if income and expense else '수입' if income else '지출' if expense else '없음'
    if unsure and amount == 0 and re.search(r'원|외상|미수|미지급',text):
        kind = '확인 필요'
    category = '판매대금' if kind == '수입' else '기타'
    for category_name, terms in [('종묘·씨앗',['모종','씨앗','종묘']),('비료·퇴비',['비료','퇴비']),('방제비',['농약','방제']),('포장비',['포장','상자']),('운송비',['운송','택배','배송']),('고용노동비',['인건비','품삯']),('농기계·수리',['수리','농기계']),('임차료',['임차','임대료'])]:
        if kind != '수입' and any(t in text for t in terms):
            category = category_name
            break
    d.update({'거래구분':kind,'분류':category,'금액(원)':amount})
    return d


def validate_record(r):
    if not isinstance(r,dict) or set(r) != set(COLS):
        raise ValueError('기록의 항목 구성이 올바르지 않습니다.')
    out = dict(r)
    for k in COLS:
        if k not in ['금액(원)','작업시간(분)'] and (not isinstance(out[k],str) or len(out[k])>10000):
            raise ValueError('문자 항목 형식을 확인하세요.')
    date.fromisoformat(out['날짜'])
    if not out['id'] or not out['작업'].strip():
        raise ValueError('ID와 작업 내용은 필수입니다.')
    if out['거래구분'] not in ['없음','수입','지출'] or out['분류'] not in CATEGORIES:
        raise ValueError('거래 구분 또는 분류가 올바르지 않습니다.')
    for k,limit in [('금액(원)',10**12),('작업시간(분)',1440)]:
        if type(out[k]) is not int or not 0 <= out[k] <= limit:
            raise ValueError(f'{k} 범위를 확인하세요.')
    if out['거래구분']=='없음' and out['금액(원)']!=0:
        raise ValueError('거래 없는 기록은 금액이 0이어야 합니다.')
    return out


def read_backup(data):
    if len(data)>10000000:
        raise ValueError('10MB 이하의 파일을 선택하세요.')
    doc=json.loads(data.decode('utf-8-sig'))
    if doc.get('version')!=2 or not isinstance(doc.get('records'),list) or len(doc['records'])>20000:
        raise ValueError('이 앱의 버전 2 백업 파일이 아닙니다.')
    rows=[validate_record(r) for r in doc['records']]
    if len({r['id'] for r in rows}) != len(rows):
        raise ValueError('백업 안에 중복 ID가 있습니다.')
    return rows


def totals(rows):
    inc=sum(r['금액(원)'] for r in rows if r['거래구분']=='수입')
    exp=sum(r['금액(원)'] for r in rows if r['거래구분']=='지출')
    return inc,exp,inc-exp


def csv_data(rows):
    buf=io.StringIO(newline='')
    writer=csv.DictWriter(buf,fieldnames=COLS)
    writer.writeheader()
    for row in rows:
        safe={k: ("'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v) for k,v in row.items()}
        writer.writerow(safe)
    return buf.getvalue().encode('utf-8-sig')


def kpis(rows):
    a,b,c=totals(rows)
    for col,label,value in zip(st.columns(3),['수입','지출','기록 기준 수지'],[a,b,c]):
        with col:
            st.metric(label,won(value),border=True)


def heading(title,subtitle):
    st.title(title)
    st.caption(subtitle)
    st.write('')


def render_home(rows):
    heading('농장의 흐름을 한눈에','작업 기록부터 수입·지출까지, 내 농장 경영노트')
    month=st.selectbox('조회 월',sorted({today_kst().strftime('%Y-%m')}|{r['날짜'][:7] for r in rows},reverse=True))
    selected=[r for r in rows if r['날짜'].startswith(month)]
    kpis(selected)
    left,right=st.columns([1.4,1])
    with left:
        with st.container(border=True):
            st.subheader('이번 달 경영 메모')
            if not selected:
                st.write('첫 기록을 남기면 이곳에 경영 현황이 표시됩니다.')
            else:
                a,b,c=totals(selected)
                st.write(f'작업 {len(selected):,}건 · 기록된 작업시간 {sum(r["작업시간(분)"] for r in selected)/60:,.1f}시간')
                expenses={cat:sum(r['금액(원)'] for r in selected if r['거래구분']=='지출' and r['분류']==cat) for cat in CATEGORIES}
                top=max(expenses,key=expenses.get)
                if expenses[top]:
                    st.write(f'가장 큰 지출은 {top}, {won(expenses[top])}입니다.')
                if c<0:
                    st.write('현재 기록에서는 지출이 수입보다 큽니다. 수확·대금 수령 시점과 누락된 기록을 함께 확인하세요.')
                else:
                    st.write('수지는 실제 입출금 기록을 기준으로 합니다. 농장의 순이익과는 다릅니다.')
                st.caption('위 메모는 저장된 숫자를 요약한 규칙 기반 설명입니다.')
    with right:
        with st.container(border=True):
            st.subheader('최근 기록')
            if not rows:
                st.caption('왼쪽 메뉴의 기록 입력에서 시작하세요.')
            for r in sorted(rows,key=lambda x:x['날짜'],reverse=True)[:4]:
                st.markdown(f"**{html.escape(r['날짜'])} · {html.escape(r['작물'] or '작물 미지정')}**")
                st.text(r['작업'][:65])
                st.caption(f"{r['거래구분']} · {won(r['금액(원)'])}" if r['거래구분']!='없음' else r['밭·장소'] or '장소 미지정')


def render_entry(rows):
    heading('오늘의 기록','말하거나 직접 입력하고, 내용을 확인한 뒤 저장하세요.')
    with st.container(border=True):
        if st.checkbox('음성 입력 사용 · 음성이 외부 Google 음성 서비스로 전송됩니다'):
            try:
                from streamlit_mic_recorder import speech_to_text
                text=speech_to_text(language='ko-KR',start_prompt='말하기 시작',stop_prompt='말하기 끝',just_once=True,key='voice_v2',use_container_width=True)
                if text:
                    st.session_state['input_note']=text
                    st.session_state['draft']=None
            except Exception:
                st.warning('음성 인식에 연결하지 못했습니다. 직접 입력하거나 휴대폰 키보드의 받아쓰기를 사용하세요.')
            st.caption('마이크 권한이 필요합니다. 새 음성 결과는 입력 내용을 교체합니다. 민감한 정보는 말하지 마세요.')
        text=st.text_area('작업과 거래 내용',key='input_note',height=110,max_chars=5000,placeholder='오늘 2번 밭에 고추 비료 두 포대 줬고, 비료값 6만 원 썼어.')
        if st.button('기록 정리',type='primary',use_container_width=True):
            if text.strip():
                st.session_state['draft']=draft_from_text(text.strip())
                st.session_state['form_version']+=1
            else:
                st.error('내용을 입력하세요.')
    d=st.session_state.get('draft')
    if not d:
        return
    st.caption('자동 정리는 규칙 기반 초안입니다. 한 기록에는 한 건의 입출금만 저장하세요. 복합 거래·단가·외상 금액은 직접 확인합니다.')
    if d.get('경고'):
        st.warning(d['경고'])
    with st.form('entry_'+str(st.session_state['form_version']),border=True):
        c1,c2,c3=st.columns(3)
        day=c1.date_input('날짜',d['날짜'])
        field=c2.text_input('밭·장소',d['밭·장소'],max_chars=100)
        crop=c3.text_input('작물',d['작물'],max_chars=100)
        task=st.text_input('작업·거래 내용 *',d['작업'] or d['원문'][:100],max_chars=500)
        c1,c2=st.columns(2)
        material=c1.text_input('자재·사용량',d['자재·사용량'],max_chars=500)
        minutes=c2.number_input('작업시간 / 분',0,1440,min(d['작업시간(분)'],1440),step=10)
        c1,c2,c3=st.columns(3)
        kinds=['없음','수입','지출','확인 필요']
        kind=c1.selectbox('실제 입출금',kinds,index=kinds.index(d['거래구분']))
        cat=c2.selectbox('거래 분류',CATEGORIES,index=CATEGORIES.index(d['분류']))
        amount=c3.number_input('총금액 / 원',min_value=0,max_value=10**12,value=d['금액(원)'],step=1000)
        memo=st.text_area('메모',max_chars=3000,height=70)
        if st.form_submit_button('저장하기',type='primary',use_container_width=True):
            if text.strip()!=d['원문']:
                st.error('입력 내용이 바뀌었습니다. 기록 정리를 다시 눌러주세요.')
            elif kind=='확인 필요':
                st.error('수입·지출 여부를 확인한 뒤 선택하세요. 입금·지급 전 금액은 현금 장부에 기록하지 마세요.')
            elif kind=='없음' and amount:
                st.error('거래가 없으면 금액을 0으로 바꿔주세요.')
            elif kind!='없음' and amount==0:
                st.error('수입·지출의 실제 총금액을 입력하세요.')
            elif not task.strip():
                st.error('작업·거래 내용을 입력하세요.')
            else:
                row={'id':str(uuid.uuid4()),'날짜':day.isoformat(),'밭·장소':field.strip(),'작물':crop.strip(),'작업':task.strip(),'자재·사용량':material.strip(),'작업시간(분)':int(minutes),'거래구분':kind,'분류':cat,'금액(원)':int(amount),'메모':memo.strip(),'원문':d['원문']}
                rows.append(validate_record(row))
                st.session_state['draft']=None
                st.session_state['notice']='저장했습니다. 사용 후 백업 파일을 내려받으세요.'
                st.rerun()


def filter_rows(rows):
    c1,c2,c3=st.columns([1,1,1.4])
    start=c1.date_input('시작일',min([date.fromisoformat(r['날짜']) for r in rows] or [today_kst().replace(day=1)]))
    end=c2.date_input('종료일',max([date.fromisoformat(r['날짜']) for r in rows]+[today_kst()]))
    query=c3.text_input('기록 검색',placeholder='작물, 밭, 작업 내용')
    if start>end:
        st.error('시작일이 종료일보다 늦습니다.')
        return []
    return [r for r in rows if start.isoformat()<=r['날짜']<=end.isoformat() and query.strip().lower() in ' '.join(str(v) for k,v in r.items() if k!='id').lower()]


def render_ledger(rows):
    heading('경영 장부','작업과 거래를 함께 확인하고 필요한 기록을 내려받으세요.')
    subset=filter_rows(rows)
    kpis(subset)
    if subset:
        frame=pd.DataFrame(subset).sort_values('날짜',ascending=False).drop(columns=['id','원문'])
        st.dataframe(frame,hide_index=True,use_container_width=True)
        st.download_button('조회 결과 CSV',csv_data(subset),'농장_조회결과.csv','text/csv')
        with st.expander('기록 삭제'):
            options={r['id']:f"{r['날짜']} · {r['작업'][:40]} · {won(r['금액(원)'])}" for r in subset}
            chosen=st.selectbox('삭제할 기록',list(options),format_func=options.get)
            confirmed=st.checkbox('선택한 기록을 삭제합니다. 삭제 전 백업을 권장합니다.')
            if st.button('선택 기록 삭제',disabled=not confirmed):
                st.session_state['records']=[r for r in rows if r['id']!=chosen]
                st.rerun()
    else:
        st.info('조회 조건에 맞는 기록이 없습니다.')


def render_analysis(rows):
    heading('경영 분석','기록된 입출금으로 비교하고, 가정한 조건으로 손익분기점을 계산합니다.')
    subset=filter_rows(rows)
    kpis(subset)
    with st.container(border=True):
        st.subheader('작물·밭별 수지')
        group=st.radio('비교 기준',['작물','밭·장소'],horizontal=True,label_visibility='collapsed')
        summary=[]
        for key in sorted({r[group] or '미지정' for r in subset}):
            selected=[r for r in subset if (r[group] or '미지정')==key]
            a,b,c=totals(selected)
            summary.append({group:key,'수입(원)':a,'지출(원)':b,'기록 기준 수지(원)':c})
        if summary:
            st.dataframe(pd.DataFrame(summary),hide_index=True,use_container_width=True)
        else:
            st.caption('기록이 쌓이면 비교할 수 있습니다.')
        st.caption('공통비 배분 전 비교입니다. 밭·작물을 지정하지 않은 비용은 미지정으로 남습니다. 수확량 대비 생산성이나 순이익을 의미하지 않습니다.')
    with st.container(border=True):
        st.subheader('손익분기점 계산')
        st.caption('한 작물·한 판매 단위·같은 생산기간 기준으로 입력하세요. 장부에서 비용을 자동 분류하지 않습니다.')
        c1,c2=st.columns(2)
        unit=c1.text_input('판매 단위',value='상자',max_chars=20)
        fixed=c2.number_input('기간 고정비 / 원',0,10**12,0,step=10000)
        c1,c2,c3=st.columns(3)
        price=c1.number_input('단위당 판매단가 / 원',0,10**9,0,step=1000)
        variable=c2.number_input('단위당 변동비 / 원',0,10**9,0,step=1000)
        qty=c3.number_input('예상 판매수량',0,10**9,0,step=1)
        contribution=price-variable
        if price==0:
            st.caption('판매단가와 비용을 입력하면 결과가 표시됩니다.')
        elif contribution<=0:
            st.warning('단위당 판매단가가 변동비보다 커야 손익분기점에 도달할 수 있습니다.')
        else:
            break_even=(fixed+contribution-1)//contribution
            st.metric('비용 회수에 필요한 최소 판매량',f'{break_even:,} {unit or "단위"}')
            st.caption('고정비 ÷ (단위당 판매단가 − 단위당 변동비), 정수 단위 올림. 고정비가 0이면 결과도 0입니다.')
            st.metric('입력 조건의 예상 손익',won(qty*contribution-fixed))
            scen=[]
            for label,ratio in [('단가 10% 하락',Decimal('.9')),('입력 단가',Decimal('1')),('단가 10% 상승',Decimal('1.1'))]:
                p=int((Decimal(price)*ratio).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
                scen.append({'가정':label,'단위당 판매단가(원)':p,'예상 손익(원)':qty*(p-variable)-fixed})
            st.dataframe(pd.DataFrame(scen),hide_index=True,use_container_width=True)
            st.caption('예측 모델이 아니라 입력한 가정에 따른 계산입니다. 노동비·감가상각 등을 빠뜨리면 손익이 과대평가될 수 있습니다.')
    a,b,c=totals(subset)
    report=f'농장 경영 요약\n작성일: {today_kst()}\n화면 조회 조건 기준 {len(subset)}건\n수입: {won(a)}\n지출: {won(b)}\n기록 기준 수지: {won(c)}\n\n작물·밭 비교\n'+ '\n'.join(str(x) for x in summary)+'\n\n실제 입출금 기록 기준. 재고, 미수금, 미지급금, 자가노동비, 감가상각, 공통비 배분을 반영한 순이익이 아닙니다.'
    st.download_button('경영 요약 내려받기',report.encode('utf-8-sig'),'농장_경영요약.txt','text/plain')


def render_backup(rows):
    heading('기록 보관','JSON은 복원용, CSV는 엑셀 확인용입니다.')
    with st.container(border=True):
        st.subheader('내 기록 내려받기')
        st.write(f'현재 기록 {len(rows):,}건')
        doc=json.dumps({'version':2,'records':rows},ensure_ascii=False,indent=2).encode('utf-8')
        a,b=st.columns(2)
        a.download_button('복원용 백업 JSON',doc,f'농장백업_{today_kst()}.json','application/json',use_container_width=True)
        b.download_button('전체 장부 CSV',csv_data(rows),f'농장장부_{today_kst()}.csv','text/csv',use_container_width=True)
    with st.container(border=True):
        st.subheader('백업 불러오기')
        file=st.file_uploader('이 버전의 JSON 백업 선택',type=['json'])
        if st.button('기록 합치기',disabled=file is None):
            try:
                imported=read_backup(file.getvalue())
                ids={r['id'] for r in rows}
                additions=[r for r in imported if r['id'] not in ids]
                rows.extend(additions)
                st.success(f'{len(additions)}건을 추가했습니다. 중복 ID는 덮어쓰지 않습니다.')
            except (ValueError,TypeError,AttributeError,UnicodeDecodeError,KeyError) as e:
                st.error(f'불러오기 실패: {e}')
        st.caption('이전 영농일지 버전의 CSV는 형식이 달라 자동 복원하지 않습니다.')
    with st.expander('저장 방식·개인정보·배포 안내'):
        st.write('기록은 현재 브라우저 접속 세션에만 존재합니다. 새로고침, 연결 종료, 서버 재시작 시 사라질 수 있습니다. 다른 접속자와 공유되지 않습니다. 장기 운영에는 인증과 사용자별 영구 저장소가 필요합니다.')
        st.write('음성 입력은 외부 Google 서비스에 음성을 전송합니다. API 키는 필요 없지만 네트워크·서비스 제한으로 실패할 수 있습니다. 글 정리와 경영 메모는 생성형 AI가 아닌 규칙 기반입니다.')
        st.write('GitHub에 app.py와 requirements.txt를 올린 뒤 Streamlit Community Cloud에서 app.py로 배포하세요. 백업 파일과 개인 장부는 공개 저장소에 올리지 마세요. Python 3.11 또는 3.12를 권장합니다.')
        st.code('pip install -r requirements.txt\nstreamlit run app.py',language='bash')


def main():
    st.set_page_config(page_title='농장노트 | 영농·경영 기록',page_icon='🌱',layout='wide')
    st.markdown('''<style>
    :root {color-scheme:light;}
    .stApp {background:#F5F7F8;color:#1E2433;}
    [data-testid="stHeader"] {background:transparent;}
    [data-testid="stSidebar"] {background:#FDFDFF;border-right:1px solid #E2E6F0;}
    .block-container {padding-top:2.4rem;padding-bottom:2rem;max-width:1250px;}
    h1 {font-size:2rem!important;font-weight:750!important;letter-spacing:-.06rem;}
    h2,h3 {letter-spacing:-.035rem;}
    h3 {font-size:1.12rem!important;}
    [data-testid="stVerticalBlockBorderWrapper"]>div {border-radius:16px!important;background:#FDFDFF;}
    [data-testid="stMetric"] {background:#FDFDFF;border-color:#E2E6F0!important;border-radius:16px;padding:20px!important;}
    [data-testid="stMetricValue"] {color:#1E2433;font-size:1.85rem;letter-spacing:-.04rem;}
    [data-testid="stMetricLabel"] {color:#505A74;}
    .stButton button,.stDownloadButton button {border-radius:10px;min-height:44px;}
    .stButton button[kind="primary"] {background:#245B43;border-color:#245B43;color:white;}
    .stTextInput input,.stTextArea textarea,.stNumberInput input {color:#1E2433;background:#FDFDFF;}
    [data-testid="stSidebar"] .stRadio>div {gap:.65rem;}
    [data-testid="stSidebar"] .stRadio label {padding:.45rem .1rem;}
    @media(max-width:640px){.block-container{padding:1.2rem .9rem;}h1{font-size:1.65rem!important;}[data-testid="stMetricValue"]{font-size:1.4rem;}}
    </style>''',unsafe_allow_html=True)
    for key,value in [('records',[]),('draft',None),('form_version',0),('input_note','')]:
        if key not in st.session_state:
            st.session_state[key]=value
    with st.sidebar:
        st.markdown('## 농장노트')
        st.caption('FARM BUSINESS NOTE')
        st.write('')
        page=st.radio('메뉴',['홈','기록 입력','경영 장부','경영 분석','백업·설정'],label_visibility='collapsed')
        st.divider()
        st.caption('개인용 · API 키 없이 시작')
        st.caption('음성 인식 + 규칙 기반 정리')
        st.caption('세션 저장 · 사용 후 백업 필수')
    if 'notice' in st.session_state:
        st.success(st.session_state.pop('notice'))
    {'홈':render_home,'기록 입력':render_entry,'경영 장부':render_ledger,'경영 분석':render_analysis,'백업·설정':render_backup}[page](st.session_state['records'])
    st.write('')
    st.caption('저장 안내: 현재 접속 세션에만 보관됩니다. 종료 전 백업·설정에서 JSON 백업을 내려받으세요. 수지는 회계상 순이익이 아닙니다.')


if __name__=='__main__':
    main()
