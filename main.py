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
from google import genai # ✨ 구버전 삭제 및 신버전(google.genai) 패키지 임포트!
from supabase import create_client, Client

# ==========================================
# 🛑 [환경 설정] 
# ==========================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ✨ 신버전 제미나이 클라이언트 생성 및 모델명 분리
ai_client = genai.Client(api_key=GEMINI_API_KEY)
AI_MODEL_NAME = 'gemini-2.5-flash' 

app = FastAPI()
user_sessions = {}

# 🛠️ 카카오 응답 템플릿
def make_kakao_reply(text, quick_replies=None):
    response = {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}
    if quick_replies: response["template"]["quickReplies"] = quick_replies
    return response

# 🌟 [신규] 텍스트 접수 확인 메뉴
TEXT_CONFIRM_REPLIES = [
    {"action": "message", "label": "✅ 예 (텍스트 접수)", "messageText": "텍스트 접수 확정"},
    {"action": "message", "label": "📸 아니요 (사진 등록)", "messageText": "사진 등록 전환"}
]

# 🌟 [1차/2차 메뉴 정의]
DEPTH_1_REPLIES = [
    {"action": "message", "label": "💥 파손 및 불량", "messageText": "파손 및 불량"},
    {"action": "message", "label": "🔢 계획 이슈", "messageText": "계획 이슈"},
    {"action": "message", "label": "💬 기타 특이사항", "messageText": "기타 특이사항"},
    {"action": "message", "label": "❌ 접수 취소", "messageText": "취소"} 
]

DAMAGE_REPLIES = [
    {"action": "message", "label": "🛋️ 제품 파손", "messageText": "제품 파손"},
    {"action": "message", "label": "📦 박스 훼손", "messageText": "박스 훼손"},
    {"action": "message", "label": "🏷️ 바코드 불량", "messageText": "바코드 불량"},
    {"action": "message", "label": "🔙 뒤로가기", "messageText": "뒤로가기"}, 
    {"action": "message", "label": "❌ 접수 취소", "messageText": "취소"}
]

PLAN_REPLIES = [
    {"action": "message", "label": "🚫 계획 미생성", "messageText": "계획 미생성"},
    {"action": "message", "label": "📉 계획 부족(실물 과다)", "messageText": "계획 부족(실물 과다)"},
    {"action": "message", "label": "📈 계획 과다(실물 부족)", "messageText": "계획 과다(실물 부족)"},
    {"action": "message", "label": "🔙 뒤로가기", "messageText": "뒤로가기"}, 
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

# ==========================================
# 🔍 [수파베이스 DB 함수] 1순위: vendor / 2순위: production_line
# ==========================================
def get_info_from_supabase(code):
    if code == "확인불가": return {"brand": "미확인", "vendor": "미확인"}
    parts = code.split('-')
    try:
        # DB에서 해당 품번 정보 전체 가져오기
        if len(parts) > 1:
            res = supabase.table('products').select('*').eq('item_code', '-'.join(parts[:-1])).eq('item_color', parts[-1]).execute()
        else:
            res = supabase.table('products').select('*').eq('item_code', code).execute()
            
        if res.data:
            row = res.data[0]
            # 브랜드 정보 추출
            brand = row.get('brand_category') or row.get('brand') or "미확인"
            
            # 🔥 1순위: vendor -> 2순위: production_line -> 3순위: 기타 대체 칼럼명 -> 최종: 미확인
            # 값이 없거나 빈 문자열("")이면 자동으로 다음 순위로 넘어갑니다.
            vendor = row.get('vendor') or row.get('production_line') or row.get('supplier') or row.get('공급업체') or "미확인"
            
            return {"brand": brand, "vendor": vendor}
            
        return {"brand": "미확인", "vendor": "미확인"}
    except Exception as e: 
        print(f"매칭 엔진 에러: {e}")
        return {"brand": "미확인", "vendor": "미확인"}

def get_employee_name(user_key):
    try:
        res = supabase.table('employees').select('name').eq('bot_key', user_key).execute()
        return res.data[0].get('name') if res.data else None 
    except Exception: return None

# ==========================================
# 🧠 [투트랙 분석 및 처리 - 콜백 백그라운드용]
# ==========================================
async def process_issue_background(user_id, issue_category, callback_url):
    print(f"⏳ [백그라운드] {issue_category} 처리 시작...")
    session = user_sessions.get(user_id, {})
    image_urls = session.get('image_urls', []) 
    manual_code = session.get('manual_code') # 🌟 텍스트 모드 확인용
    
    p_code = "확인불가"
    uploaded_urls = []
    is_text_track = bool(manual_code) # 사진 없이 텍스트로 들어왔는지 판단!

    if is_text_track:
        print("📝 [트랙 B] 텍스트 수동 접수 모드 진행 중...")
        p_code = manual_code.strip().upper() # AI 분석 스킵하고 그대로 품번으로 사용!
        
    else:
        print("📸 [트랙 A] 사진 AI 분석 모드 진행 중...")
        imgs_for_ai = []
        for url in image_urls:
            async with httpx.AsyncClient() as client:
                img_response = await client.get(url)
                img = Image.open(io.BytesIO(img_response.content))
                img = ImageOps.exif_transpose(img)
                img.thumbnail((1600, 1600))
                imgs_for_ai.append(img)

        user_text = issue_category  
        prompt_1 = f"""
        당신은 최고 수준의 물류 SCM 라벨 판독기입니다. 작업자의 메시지('{user_text}')와 첨부된 여러 장의 사진을 분석하여 오직 JSON 형식으로만 응답하세요.
        여러 장의 사진 중 바코드가 가장 명확하게 보이는 사진을 찾아 아래 규칙대로 판독하세요.

        [핵심 추출 규칙]
        1. 사진의 바코드 주변에서 '품목코드'와 '색상코드'를 찾아 반드시 중간에 하이픈(-)을 넣어 "품목코드-색상코드" 형태로 결합하세요.
        2. 🚨예외 규칙: 만약 품목코드 자체에 이미 하이픈(-)과 색상코드가 결합되어 있다면 별도로 적힌 색상코드는 무시하세요.

        [절대 무시 규칙] 괄호 기호 안의 내용, 생산일자, 벤더 영문 코드, 로트 번호 등 무시.
        [판독 족보] 입력: "HSOC1140DTRA 2026-03-16 F", 옆에 "WW" -> 💡정답: {{"product_code": "HSOC1140DTRA-WW"}}

        오직 아래 JSON 형식으로만 응답하세요:
        {{"product_code": "추출된코드"}}
        """
        ai_contents = [prompt_1] + imgs_for_ai
        
        # ✨ 신버전 API 호출 방식으로 완벽 이식 (client.models.generate_content)
        ai_response = await asyncio.to_thread(
            ai_client.models.generate_content,
            model=AI_MODEL_NAME,
            contents=ai_contents
        )
        
        match = re.search(r'\{.*?\}', ai_response.text.strip(), re.DOTALL)
        p_code = json.loads(match.group()).get("product_code", "확인불가") if match else "확인불가"
        p_code = str(p_code).strip().upper()

    # --- 여기서부터는 투트랙 공통 DB 처리 영역 ---
    is_valid = await asyncio.to_thread(check_code_in_supabase, p_code)

    if p_code == "확인불가":
        final_msg = make_kakao_reply("⚠️ 바코드 인식 실패! 명확히 보이는 사진을 다시 전송해 주세요.")
    elif not is_valid and not is_text_track:
        final_msg = make_kakao_reply(f"⚠️ DB 매칭 실패! 마스터 DB에 없는 코드입니다. 수동 접수 필요. (AI 판독: {p_code})")
    elif not is_valid and is_text_track:
        final_msg = make_kakao_reply(f"⚠️ DB 매칭 실패! 마스터 DB에 없는 코드입니다. 오타가 없는지 확인해 주세요. (입력한 코드: {p_code})")
    else:
        info = await asyncio.to_thread(get_info_from_supabase, p_code)
        emp_name = await asyncio.to_thread(get_employee_name, user_id)
        reporter_name = emp_name if emp_name else f"미등록({user_id[-4:]})"
        
        now_str = datetime.now().strftime("%Y%m%d")
        reception_id = f"LOG-{now_str}-{str(uuid.uuid4())[:8].upper()}"
        
        # 텍스트 트랙일 때는 이미지 업로드 과정을 건너뜁니다!
        if not is_text_track:
            for idx, img in enumerate(imgs_for_ai):
                img.thumbnail((800, 800))
                output_io = io.BytesIO()
                if img.mode != 'RGB': img = img.convert('RGB')
                img.save(output_io, format="JPEG", quality=80)
                file_path = f"{reception_id}_{idx+1}.jpg"
                supabase.storage.from_("issue_images").upload(path=file_path, file=output_io.getvalue(), file_options={"upsert": "true"})
                uploaded_urls.append(supabase.storage.from_("issue_images").get_public_url(file_path))
            
        final_image_string = ",".join(uploaded_urls) if uploaded_urls else ""

        supabase.table("logistics_issues").insert({
            "reception_no": reception_id, "brand": info["brand"], "vendor": info["vendor"],
            "product_code": p_code, "issue_type": issue_category, "reporter": reporter_name,
            "status": "조치대기", "image_url": final_image_string, "chat_id": user_id 
        }).execute()
        
        track_badge = "📝 텍스트 접수" if is_text_track else "📸 사진 접수"
        final_msg = make_kakao_reply(f"✔️ 이슈 접수 완료! ({track_badge})\n👤 담당: {reporter_name}\n📦 품번: {p_code}\n🏢 공급사: {info['vendor']}\n💬 사유: {issue_category}")

    async with httpx.AsyncClient() as client:
        await client.post(callback_url, json=final_msg)
    user_sessions.pop(user_id, None)
    print(f"✅ 결과 전송 완료! (판독: {p_code})")


# ==========================================
# 📡 [API 엔드포인트] (🚨 강제 실명제 + 투트랙 적용!)
# ==========================================
@app.post("/api/kakao")
async def kakao_webhook(request: Request, background_tasks: BackgroundTasks):
    req_data = await request.json()
    user_id = req_data["userRequest"]["user"]["properties"]["botUserKey"]
    utterance = req_data["userRequest"]["utterance"].strip()
    callback_url = req_data["userRequest"].get("callbackUrl")

    # 🟢 1. 프리패스 (이름 등록)
    if utterance.startswith("등록 "):
        new_name = utterance.replace("등록 ", "").strip()
        if new_name:
            supabase.table('employees').upsert({"bot_key": user_id, "name": new_name}).execute()
            return make_kakao_reply(f"✅ 반갑습니다, {new_name}님! 이제 정상적으로 이용하실 수 있습니다.")

    # 🛑 2. 입국 심사대 (이름 없으면 컷)
    emp_name = get_employee_name(user_id) 
    if not emp_name:
        return make_kakao_reply("🚫 시스템 접근 권한이 없습니다.\n채팅창에 아래와 같이 실명을 1회 등록해 주세요!\n👉 예시: 등록 홍길동")

    # 🛑 3. 공통 제어 명령 (취소/뒤로가기)
    if utterance == "취소":
        user_sessions.pop(user_id, None) 
        return make_kakao_reply("🔄 접수 진행이 취소되었습니다.\n현장사진을 등록해주세요.")

    elif utterance == "뒤로가기":
        if user_id not in user_sessions or not (user_sessions[user_id].get("image_urls") or user_sessions[user_id].get("manual_code")):
            return make_kakao_reply("⚠️ 돌아갈 내역이 없습니다. 사진이나 코드를 먼저 입력해 주세요.")
        user_sessions[user_id]["waiting_etc"] = False 
        return make_kakao_reply("🔙 이슈 유형을 다시 선택해 주세요.", DEPTH_1_REPLIES)

    # 🌟 [신규 트랙 B] 텍스트 접수 확정 시
    elif utterance == "텍스트 접수 확정":
        if user_id in user_sessions and user_sessions[user_id].get("pending_manual_code"):
            user_sessions[user_id]["manual_code"] = user_sessions[user_id]["pending_manual_code"] # 임시 코드를 찐 코드로 저장!
            return make_kakao_reply("✔️ 텍스트 접수가 선택되었습니다.\n이슈 유형을 선택해 주세요.", DEPTH_1_REPLIES)
        return make_kakao_reply("⚠️ 오류가 발생했습니다. 코드를 다시 입력해 주세요.")

    # 🌟 [신규 트랙 B] 사진 등록 전환 (아니요 선택 시)
    elif utterance == "사진 등록 전환":
        user_sessions.pop(user_id, None)
        return make_kakao_reply("📸 현장사진을 등록해주세요.")

    # 📸 [트랙 A] 다중 사진 담기
    elif utterance.startswith("http"):
        if user_id not in user_sessions: user_sessions[user_id] = {"image_urls": []}
        # 텍스트 모드로 진행 중이었다면 엎어버리고 사진 모드로 전환!
        if "manual_code" in user_sessions[user_id]: user_sessions[user_id].pop("manual_code") 
        
        user_sessions[user_id]["image_urls"].append(utterance)
        count = len(user_sessions[user_id]["image_urls"])
        return make_kakao_reply(f"📸 {emp_name}님, {count}번째 사진 수집 완료!\n더 보내시거나, 완료 시 버튼을 눌러주세요.", [{"action": "message", "label": "✅ 사진 전송 완료", "messageText": "사진 전송 완료"}])

    elif utterance == "사진 전송 완료":
        if user_id not in user_sessions or not user_sessions[user_id].get("image_urls"): return make_kakao_reply("⚠️ 사진을 먼저 보내주세요!")
        return make_kakao_reply(f"🛒 총 {len(user_sessions[user_id]['image_urls'])}장의 사진 수집 완료.\n이슈 유형을 선택해 주세요.", DEPTH_1_REPLIES)

    # 🔽 카테고리 뎁스 전개
    elif utterance == "파손 및 불량":
        return make_kakao_reply("💥 품질 관련 이슈군요!\n상세 사유를 골라주세요.", DAMAGE_REPLIES)
    elif utterance == "계획 이슈":
        return make_kakao_reply("🔢 수량/계획 관련 이슈군요!\n상세 사유를 골라주세요.", PLAN_REPLIES)
    elif utterance == "기타 특이사항":
        user_sessions[user_id]["waiting_etc"] = True
        return make_kakao_reply("💬 상세 내용을 채팅창에 직접 입력해 주세요.\n(예: 라벨이 심하게 젖어 있음)")

    # 🏁 찐막: 백그라운드 AI 실행 트리거
    FINAL_ISSUES = ["제품 파손", "박스 훼손", "바코드 불량", "계획 미생성", "계획 부족(실물 과다)", "계획 과다(실물 부족)"]
    
    if utterance in FINAL_ISSUES or (user_id in user_sessions and user_sessions[user_id].get("waiting_etc")):
        if user_id not in user_sessions or not (user_sessions[user_id].get("image_urls") or user_sessions[user_id].get("manual_code")):
            return make_kakao_reply("⚠️ 접수할 사진이나 코드가 없습니다. 현장사진을 등록해주세요.")
        
        background_tasks.add_task(process_issue_background, user_id, utterance, callback_url)
        return {"version": "2.0", "useCallback": True}

    # 🌟 [신규 트랙 B] 사진, 메뉴 버튼이 아닌 "아무 텍스트(품번)"나 쳤을 때! (Catch-all)
    else:
        user_sessions[user_id] = {"pending_manual_code": utterance} # 일단 임시로 담아둠
        return make_kakao_reply(
            f"💬 입력하신 내용: [ {utterance} ]\n\n사진 없이 위 텍스트(품번)로만 접수하시겠습니까?",
            TEXT_CONFIRM_REPLIES
        )
