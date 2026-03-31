import os
import io
import asyncio
import re
import uuid
import json
import httpx
from datetime import datetime
from PIL import Image, ImageOps
from fastapi import FastAPI, Request, BackgroundTasks
import google.generativeai as genai
from supabase import create_client, Client

# ==========================================
# 🛑 [환경 설정] 
# ==========================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash') 

app = FastAPI()
user_sessions = {}

# 🛠️ 카카오 응답 템플릿
def make_kakao_reply(text, quick_replies=None):
    response = {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}
    if quick_replies: response["template"]["quickReplies"] = quick_replies
    return response

# 🌟 [1차 메뉴 - 대분류]
DEPTH_1_REPLIES = [
    {"action": "message", "label": "💥 파손 및 불량", "messageText": "파손 및 불량"},
    {"action": "message", "label": "🔢 계획 이슈", "messageText": "계획 이슈"},
    {"action": "message", "label": "💬 기타 특이사항", "messageText": "기타 특이사항"},
    {"action": "message", "label": "❌ 접수 취소", "messageText": "취소"} # 취소 추가!
]

# 🌟 [2차 메뉴 - 품질]
DAMAGE_REPLIES = [
    {"action": "message", "label": "🛋️ 제품 파손", "messageText": "제품 파손"},
    {"action": "message", "label": "📦 박스 훼손", "messageText": "박스 훼손"},
    {"action": "message", "label": "🏷️ 바코드 불량", "messageText": "바코드 불량"},
    {"action": "message", "label": "🔙 뒤로가기", "messageText": "뒤로가기"}, # 뒤로가기 추가!
    {"action": "message", "label": "❌ 접수 취소", "messageText": "취소"}
]

# 🌟 [2차 메뉴 - 물류]
PLAN_REPLIES = [
    {"action": "message", "label": "🚫 계획 미생성", "messageText": "계획 미생성"},
    {"action": "message", "label": "📉 계획 부족(실물 과다)", "messageText": "계획 부족(실물 과다)"},
    {"action": "message", "label": "📈 계획 과다(실물 부족)", "messageText": "계획 과다(실물 부족)"},
    {"action": "message", "label": "🔙 뒤로가기", "messageText": "뒤로가기"}, # 뒤로가기 추가!
    {"action": "message", "label": "❌ 접수 취소", "messageText": "취소"}
]

# ==========================================
# 🔍 [수파베이스 DB 함수]
# ==========================================
def check_code_in_supabase(full_code):
    if full_code == "확인불가": return False
    parts = full_code.split('-')
    try:
        if len(parts) > 1:
            res = supabase.table('products').select('item_code').eq('item_code', '-'.join(parts[:-1])).eq('item_color', parts[-1]).execute()
        else:
            res = supabase.table('products').select('item_code').eq('item_code', full_code).execute()
        return len(res.data) > 0
    except Exception: return False

def get_info_from_supabase(code):
    if code == "확인불가": return {"brand": "미확인", "vendor": "미확인"}
    parts = code.split('-')
    try:
        if len(parts) > 1:
            res = supabase.table('products').select('brand_category, vendor').eq('item_code', '-'.join(parts[:-1])).eq('item_color', parts[-1]).execute()
        else:
            res = supabase.table('products').select('brand_category, vendor').eq('item_code', code).execute()
        if res.data:
            row = res.data[0]
            return {"brand": row.get('brand_category') or "미확인", "vendor": row.get('vendor') or "미확인"}
        return {"brand": "미확인", "vendor": "미확인"}
    except Exception: return {"brand": "미확인", "vendor": "미확인"}

def get_employee_name(user_key):
    try:
        res = supabase.table('employees').select('name').eq('bot_key', user_key).execute()
        return res.data[0].get('name') if res.data else None 
    except Exception: return None

# ==========================================
# 🧠 [AI 분석 및 처리 - 콜백 백그라운드용]
# ==========================================
async def process_issue_background(user_id, issue_category, callback_url):
    print(f"⏳ [백그라운드] {issue_category} 분석 시작...")
    session = user_sessions.get(user_id, {})
    image_urls = session.get('image_urls', []) 
    
    imgs_for_ai = []
    for url in image_urls:
        async with httpx.AsyncClient() as client:
            img_response = await client.get(url)
            img = Image.open(io.BytesIO(img_response.content))
            # 🔥 EXIF 방향 정보를 읽어서 똑바로 세워주는 마법의 한 줄!
            img = ImageOps.exif_transpose(img)
            img.thumbnail((1600, 1600))
            imgs_for_ai.append(img)

    # 🔥 기훈님의 완벽한 족보 프롬프트 전면 복구!
    user_text = issue_category  
    prompt_1 = f"""
    당신은 최고 수준의 물류 SCM 라벨 판독기입니다. 작업자의 메시지('{user_text}')와 첨부된 여러 장의 사진을 분석하여 오직 JSON 형식으로만 응답하세요.
    여러 장의 사진이 주어질 경우, 그 중 바코드가 가장 명확하게 보이는 사진을 찾아 아래 규칙대로 판독하세요.

    [핵심 추출 규칙]
    1. 사진의 바코드 주변에서 '품목코드'와 '색상코드'를 찾아 반드시 중간에 하이픈(-)을 넣어 "품목코드-색상코드" 형태로 결합하세요.
    2. 🚨예외 규칙: 만약 품목코드 자체에 이미 하이픈(-)과 색상코드가 결합되어 있다면 (예: FXXX2602082-1P1), 별도로 적힌 색상코드는 무시하고 그 완성된 문자열 자체를 정답으로 사용하세요.

    [절대 무시(제외) 규칙 - 아래 항목은 쳐다보지도 마세요!]
    - 괄호 기호 `()` 및 괄호 안의 모든 내용 무시 (예: (2026.03.16), (H26119-GAON), (C26317), (F26303) 등)
    - 생산일자 무시 (예: 2026-03-16, 2026.03.17, 26.03.17 등)
    - 벤더/공급업체 영문 코드 무시 (예: F, GAON, WELLSEAT, JF, Y, TO, NCC, LSF, YJ 등)
    - 로트(LOT) 번호 무시 (예: P260311, P26311, C26317, H26119 등)
    - 제품 시리즈명, 무게, 수출명 무시 (예: VIM, UY54, 20, PCS/box 등)

    [🎯 판독 족보 (이 패턴을 완벽하게 모방하세요!)]
    - 입력: "HSOC1140DTRA 2026-03-16 F", 옆에 "WW" -> 💡정답: {{"product_code": "HSOC1140DTRA-WW"}}
    - 입력: "HBA50E04PBL(H26119-GAON)", 옆에 "SP" -> 💡정답: {{"product_code": "HBA50E04PBL-SP"}}
    - 입력: "IBH11AN00A(2026.03.16) WELLSEAT", 옆에 "5K1L" -> 💡정답: {{"product_code": "IBH11AN00A-5K1L"}}
    - 입력: "HSOD0214XN(F26303)", 옆에 "PW" -> 💡정답: {{"product_code": "HSOD0214XN-PW"}}

    오직 아래 JSON 형식으로만 응답하세요:
    {{"product_code": "추출된코드"}}
    """
    
    # 여러 장의 사진(imgs_for_ai 리스트)과 프롬프트를 한방에 AI에게 쏩니다!
    ai_contents = [prompt_1] + imgs_for_ai
    ai_response = await asyncio.to_thread(model.generate_content, ai_contents)
    match = re.search(r'\{.*?\}', ai_response.text.strip(), re.DOTALL)
    p_code = json.loads(match.group()).get("product_code", "확인불가") if match else "확인불가"
    p_code = str(p_code).strip().upper()

    is_valid = await asyncio.to_thread(check_code_in_supabase, p_code)

    if p_code == "확인불가":
        final_msg = make_kakao_reply("⚠️ 바코드 인식 실패! 바코드가 명확히 보이는 사진을 포함하여 다시 전송해 주세요.")
    elif not is_valid:
        final_msg = make_kakao_reply(f"⚠️ DB 매칭 실패! 마스터 DB에 없는 코드입니다. 수동 접수 필요. (AI가 읽은 글자: {p_code})")
    else:
        info = await asyncio.to_thread(get_info_from_supabase, p_code)
        emp_name = await asyncio.to_thread(get_employee_name, user_id)
        reporter_name = emp_name if emp_name else f"미등록({user_id[-4:]})"
        
        now_str = datetime.now().strftime("%Y%m%d")
        reception_id = f"LOG-{now_str}-{str(uuid.uuid4())[:8].upper()}"
        
        uploaded_urls = []
        for idx, img in enumerate(imgs_for_ai):
            img.thumbnail((800, 800))
            output_io = io.BytesIO()
            if img.mode != 'RGB': img = img.convert('RGB')
            img.save(output_io, format="JPEG", quality=80)
            file_path = f"{reception_id}_{idx+1}.jpg"
            supabase.storage.from_("issue_images").upload(path=file_path, file=output_io.getvalue(), file_options={"upsert": "true"})
            uploaded_urls.append(supabase.storage.from_("issue_images").get_public_url(file_path))
            
        supabase.table("logistics_issues").insert({
            "reception_no": reception_id, "brand": info["brand"], "vendor": info["vendor"],
            "product_code": p_code, "issue_type": issue_category, "reporter": reporter_name,
            "status": "조치대기", "image_url": ",".join(uploaded_urls), "chat_id": user_id 
        }).execute()
        
        final_msg = make_kakao_reply(f"✔️ 이슈 접수 완료!\n👤 담당: {reporter_name}\n📦 품번: {p_code}\n🏢 공급사: {info['vendor']}\n💬 사유: {issue_category}")

    async with httpx.AsyncClient() as client:
        await client.post(callback_url, json=final_msg)
    user_sessions.pop(user_id, None)
    print(f"✅ 결과 전송 완료! (판독: {p_code})")


# ==========================================
# 📡 [API 엔드포인트] (🚨 강제 실명제 적용 버전!)
# ==========================================
@app.post("/api/kakao")
async def kakao_webhook(request: Request, background_tasks: BackgroundTasks):
    req_data = await request.json()
    user_id = req_data["userRequest"]["user"]["properties"]["botUserKey"]
    utterance = req_data["userRequest"]["utterance"].strip()
    callback_url = req_data["userRequest"].get("callbackUrl")

    # 🟢 1. 프리패스 구역 (이름 등록하러 온 사람은 무조건 통과!)
    if utterance.startswith("등록 "):
        new_name = utterance.replace("등록 ", "").strip()
        if new_name:
            supabase.table('employees').upsert({"bot_key": user_id, "name": new_name}).execute()
            return make_kakao_reply(f"✅ 반갑습니다, {new_name}님! 이제 SCM 헬퍼의 모든 기능을 정상적으로 이용하실 수 있습니다.")

    # 🛑 2. 철통 보안 입국 심사대 (이름 없으면 여기서 다 쫓겨남!)
    emp_name = get_employee_name(user_id) 
    if not emp_name:
        return make_kakao_reply(
            "🚫 시스템 접근 권한이 없습니다.\n\n"
            "이슈 접수를 위해 최초 1회 실명 등록이 필수입니다.\n"
            "채팅창에 아래와 같이 입력해 주세요!\n\n"
            "👉 예시: 등록 홍길동"
        )

    # 🛑 [신규 기능] 취소 버튼을 눌렀을 때
    if utterance == "취소":
        user_sessions.pop(user_id, None) # 장바구니 확 비워버림
        return make_kakao_reply("🔄 접수 진행이 취소되고 장바구니가 비워졌습니다.\n처음부터 다시 사진을 전송해 주세요.")

    # 🔙 [신규 기능] 뒤로가기 버튼을 눌렀을 때
    elif utterance == "뒤로가기":
        if user_id not in user_sessions or not user_sessions[user_id].get("image_urls"):
            return make_kakao_reply("⚠️ 돌아갈 내역이 없습니다. 사진을 먼저 보내주세요.")
        
        # 기타 특이사항 입력 대기 상태였다면 그것도 얌전하게 해제
        user_sessions[user_id]["waiting_etc"] = False 
        
        # 사진은 유지한 채로, 1차 대분류 메뉴를 다시 쏴줌!
        return make_kakao_reply(f"🛒 사진 {len(user_sessions[user_id]['image_urls'])}장 유지됨.\n이슈 유형을 다시 선택해 주세요.", DEPTH_1_REPLIES)

    # ========================================================
    # 🔽 여기서부터는 '입국 심사를 통과한(등록된)' 작업자만 들어올 수 있습니다!
    # ========================================================

    # 3. 다중 사진 장바구니 담기
    if utterance.startswith("http"):
        if user_id not in user_sessions: user_sessions[user_id] = {"image_urls": []}
        user_sessions[user_id]["image_urls"].append(utterance)
        count = len(user_sessions[user_id]["image_urls"])
        return make_kakao_reply(f"📸 {emp_name}님, {count}번째 사진 수집 완료!\n더 보내시거나, 다 보내셨다면 아래 버튼을 눌러주세요.", [{"action": "message", "label": "✅ 사진 전송 완료", "messageText": "사진 전송 완료"}])

    # 4. 사진 전송 완료 -> 대분류 노출
    elif utterance == "사진 전송 완료":
        if user_id not in user_sessions: return make_kakao_reply("⚠️ 사진을 먼저 보내주세요!")
        return make_kakao_reply(f"🛒 총 {len(user_sessions[user_id]['image_urls'])}장의 사진이 수집되었습니다.\n이슈 유형을 선택해 주세요.", DEPTH_1_REPLIES)

    # 5. 대분류 선택 -> 소분류 노출
    elif utterance == "파손 및 불량":
        return make_kakao_reply("💥 품질 관련 이슈군요!\n상세 사유를 골라주세요.", DAMAGE_REPLIES)
    elif utterance == "계획 이슈":
        return make_kakao_reply("🔢 수량/계획 관련 이슈군요!\n상세 사유를 골라주세요.", PLAN_REPLIES)
    
    # 6. [기타 특이사항] 처리
    elif utterance == "기타 특이사항":
        user_sessions[user_id]["waiting_etc"] = True
        return make_kakao_reply("💬 상세 내용을 채팅창에 직접 입력해 주세요.\n(예: 라벨이 심하게 젖어 있음, 박스가 개봉되어 있음 등)")

    # 7. 최종 소분류 선택 혹은 기타 텍스트 입력 시 -> AI 분석 시작!
    FINAL_ISSUES = ["제품 파손", "박스 훼손", "바코드 불량", "계획 미생성", "계획 부족(실물 과다)", "계획 과다(실물 부족)"]
    
    if utterance in FINAL_ISSUES or (user_id in user_sessions and user_sessions[user_id].get("waiting_etc")):
        if user_id not in user_sessions: return make_kakao_reply("⚠️ 사진을 먼저 보내주세요!")
        background_tasks.add_task(process_issue_background, user_id, utterance, callback_url)
        return {"version": "2.0", "useCallback": True}

    else:
        # 쓸데없는 말 치면 안내 멘트 (이름까지 불러줌!)
        return make_kakao_reply(f"안녕하세요 {emp_name}님! SCM 헬퍼입니다.\n이슈 접수를 원하시면 현장 사진을 전송해 주세요.")