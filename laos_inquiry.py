"""라오스 골프 라운지 상담 신청 → 카카오톡(나에게 보내기) 알림.

Supabase inquiries 테이블에서 아직 알림 안 보낸 신청(notified_at 비어 있음)을 읽어
카톡으로 보내고, 보낸 건은 notified_at을 채운다.
카카오 토큰 처리는 alert.py의 함수를 그대로 쓴다 (같은 토큰·같은 갱신 방식).

필요한 값
- LAOS_SUPABASE_URL     : 워크플로에 적힌 프로젝트 주소
- LAOS_SUPABASE_SECRET  : GitHub Secret (Supabase sb_secret_ 키, 절대 공개 금지)
- KAKAO_REST_KEY / KAKAO_CLIENT_SECRET / KAKAO_REFRESH_TOKEN : 기존 신규개설 알림과 같은 Secret
"""
import json, os, sys, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

from alert import kakao_token, kakao_send, load_refresh

KST = timezone(timedelta(hours=9))
URL = os.environ.get("LAOS_SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("LAOS_SUPABASE_SECRET", "")
DASH = os.environ.get("LAOS_DASH_LINK", "https://supabase.com/dashboard")
RECIPIENT = "KAKAO_REFRESH_TOKEN"  # 업무 알림이라 '나'에게만 보낸다


def sb(method, path, body=None):
    req = urllib.request.Request(f"{URL}/rest/v1/{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"apikey": KEY, "Content-Type": "application/json",
                                          "Prefer": "return=minimal"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def fmt(row):
    t = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).astimezone(KST)
    lines = [f"📝 라오스 골프 상담 신청 ({t:%m/%d %H:%M})",
             f"{row.get('name', '')} · {row.get('phone', '')}",
             f"{row.get('depart') or '-'} 출발 · {row.get('period') or '시기 미정'} · {row.get('party') or '-'}명"]
    if row.get("room"):
        lines.append(f"모임 방: {row['room']}")
    if row.get("memo"):
        lines.append(f"메모: {row['memo']}")
    return "\n".join(lines)[:200]


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if not (URL and KEY):
        print("::notice::LAOS_SUPABASE_SECRET 이 아직 없어요. 설정 전까지는 건너뜁니다.")
        return
    if mode == "test":
        token = kakao_token(RECIPIENT)
        kakao_send(token, "📝 라오스 라운지 상담 알림 테스트\n이 메시지가 보이면 정상이에요.", DASH)
        print("[테스트] 발송 완료")
        return
    rows = sb("GET", "inquiries?select=*&notified_at=is.null&order=id.asc&limit=20") or []
    print(f"[조회] 새 상담 신청 {len(rows)}건")
    if not rows:
        return
    if mode == "dry":
        for r in rows:
            print("----\n" + fmt(r))
        return
    if not load_refresh(RECIPIENT):
        print("::error::카카오 토큰이 없어요")
        sys.exit(1)
    token = kakao_token(RECIPIENT)
    sent = []
    for r in rows:
        try:
            kakao_send(token, fmt(r), DASH)
            sent.append(r["id"])
        except Exception as e:
            print(f"::warning::{r['id']}번 발송 실패: {e}")
    if sent:
        now = datetime.now(timezone.utc).isoformat()
        sb("PATCH", f"inquiries?id=in.({','.join(str(i) for i in sent)})", {"notified_at": now})
        print(f"[발송] {len(sent)}건 카톡 전송 · 표시 완료")


if __name__ == "__main__":
    main()
