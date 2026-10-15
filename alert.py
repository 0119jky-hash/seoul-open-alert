"""서울 신규 약국·의원·병원 인허가 → 카카오톡 '나에게 보내기' 알림.

GitHub Actions에서 하루 한 번 실행된다.
  python alert.py          정상 실행 (조회 → 필터 → 카톡 발송 → 상태 저장)
  python alert.py --probe  API 응답 구조만 출력 (발송 없음)
  python alert.py --dry    발송 없이 보낼 내용만 출력
"""
import json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
SEEN_PATH = os.path.join(ROOT, "state", "seen.json")
SERVICE_KEY = os.environ.get("DATA_GO_KR_KEY", "")
PAGE_SIZE = 1000

# 필드 이름은 API 버전마다 달라서 후보를 순서대로 찾는다.
F_ID = ["ykiho", "MNG_NO", "mgtNo", "MGT_NO", "관리번호"]
F_NAME = ["yadmNm", "BPLC_NM", "bplcNm", "사업장명"]
F_DATE = ["estbDd", "LCPMT_YMD", "apvPermYmd", "APV_PERM_YMD", "인허가일자"]
F_STATUS = ["SALS_STTS_NM", "trdStateNm", "TRD_STATE_NM", "DTL_SALS_STTS_NM", "dtlStateNm", "영업상태명"]
F_ROAD = ["addr", "ROAD_NM_ADDR", "rdnWhlAddr", "RDN_WHL_ADDR", "도로명전체주소"]
F_LOT = ["LOTNO_ADDR", "siteWhlAddr", "SITE_WHL_ADDR", "소재지전체주소"]
F_KIND = ["clCdNm", "BZSTAT_SE_NM", "uptaeNm", "UPTAE_NM", "MDCL_INST_SE_NM", "업태구분명", "의료기관종별명"]


def pick(row, keys):
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def http_get_json(url, params):
    q = urllib.parse.urlencode(params, safe="%")
    req = urllib.request.Request(f"{url}?{q}", headers={"Accept": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode("utf-8", "replace")
            return json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f"JSON이 아닌 응답: {body[:400]}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:600]
            raise RuntimeError(f"HTTP {e.code}: {detail}")
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))


def find_rows(obj):
    """응답 어디에 있든 dict 목록(=데이터 행)을 찾아 돌려준다."""
    if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
        return obj
    if isinstance(obj, dict):
        for k in ("item", "items", "row", "data", "body", "response", "result"):
            if k in obj:
                found = find_rows(obj[k])
                if found is not None:
                    return found
        for v in obj.values():
            found = find_rows(v)
            if found is not None:
                return found
    return None


def find_total(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in ("totalcount", "total_count", "list_total_count"):
                try:
                    return int(v)
                except Exception:
                    pass
            t = find_total(v)
            if t is not None:
                return t
    return None


def ep(typ):
    v = CONFIG.get("endpoints", {}).get(typ)
    if isinstance(v, str):
        return v, {}
    return (v or {}).get("url"), (v or {}).get("params", {})


def fetch_all(url, extra=None):
    rows, page = [], 1
    while True:
        data = http_get_json(url, {"serviceKey": SERVICE_KEY, "pageNo": page,
                                   "numOfRows": PAGE_SIZE, "returnType": "json", "type": "json", **(extra or {})})
        batch = find_rows(data) or []
        rows.extend(batch)
        total = find_total(data)
        if not batch or len(batch) < PAGE_SIZE or (total is not None and len(rows) >= total):
            break
        page += 1
    return rows


def gu_of(addr):
    m = re.search(r"서울(?:특별시|시)?\s+(\S+?구)\b", addr)
    return m.group(1) if m else ""


def ymd(s):
    d = re.sub(r"\D", "", s)[:8]
    return d if len(d) == 8 else ""


def is_open(status):
    return not any(w in status for w in ("폐업", "취소", "말소", "휴업"))


def collect():
    since = (datetime.now(KST) - timedelta(days=CONFIG.get("lookback_days", 7))).strftime("%Y%m%d")
    want_gu = set(CONFIG.get("gu") or [])
    out = []
    for typ in CONFIG.get("types", []):
        url, extra = ep(typ)
        if not url:
            print(f"[건너뜀] {typ}: endpoints에 API 주소가 없음")
            continue
        try:
            rows = fetch_all(url, extra)
        except Exception as e:
            print(f"[오류] {typ} 조회 실패: {e}")
            continue
        print(f"[조회] {typ}: 전체 {len(rows)}건")
        for r in rows:
            addr = pick(r, F_ROAD) or pick(r, F_LOT)
            gu = gu_of(addr) or gu_of(pick(r, F_LOT))
            if not gu or (want_gu and gu not in want_gu):
                continue
            d = ymd(pick(r, F_DATE))
            if not d or d < since or not is_open(pick(r, F_STATUS)):
                continue
            kind = pick(r, F_KIND)
            # 의원/병원이 한 API에 섞여 오는 경우 업종명으로 다시 거른다.
            label = typ
            if kind and typ in ("의원", "병원") and typ not in kind:
                continue
            ident = pick(r, F_ID) or f"{typ}|{pick(r, F_NAME)}|{addr}"
            out.append({"id": ident, "type": label, "name": pick(r, F_NAME), "gu": gu,
                        "date": d, "addr": addr})
    out.sort(key=lambda x: (x["date"], x["gu"]), reverse=True)
    return out


# ---------- 카카오 ----------
def kakao_token():
    body = {"grant_type": "refresh_token",
            "client_id": os.environ["KAKAO_REST_KEY"],
            "refresh_token": os.environ["KAKAO_REFRESH_TOKEN"]}
    if os.environ.get("KAKAO_CLIENT_SECRET"):
        body["client_secret"] = os.environ["KAKAO_CLIENT_SECRET"]
    req = urllib.request.Request("https://kauth.kakao.com/oauth/token",
                                 data=urllib.parse.urlencode(body).encode())
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.loads(r.read())
    if tok.get("refresh_token"):
        # 리프레시 토큰이 새로 나오면 워크플로가 GitHub Secret을 갱신한다.
        open(os.path.join(ROOT, ".new_refresh_token"), "w").write(tok["refresh_token"])
        print("[카카오] 리프레시 토큰 갱신됨")
    return tok["access_token"]


def kakao_send(token, text, link):
    tmpl = {"object_type": "text", "text": text[:200],
            "link": {"web_url": link, "mobile_web_url": link}, "button_title": "목록 보기"}
    req = urllib.request.Request("https://kapi.kakao.com/v2/api/talk/memo/default/send",
                                 data=urllib.parse.urlencode({"template_object": json.dumps(tmpl, ensure_ascii=False)}).encode(),
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read())
    if res.get("result_code") != 0:
        raise RuntimeError(f"카톡 발송 실패: {res}")


def line(it):
    return f"[{it['type']}] {it['name']}\n{it['gu']} · {it['date'][4:6]}/{it['date'][6:]} 인허가\n{it['addr']}"


def pack(items, header):
    """200자 제한에 맞춰 여러 메시지로 나눈다."""
    msgs, cur = [], header
    for it in items:
        piece = line(it)
        if len(piece) > 180:
            piece = piece[:177] + "…"
        if len(cur) + 2 + len(piece) > 200:
            msgs.append(cur)
            cur = piece
        else:
            cur = f"{cur}\n\n{piece}"
    msgs.append(cur)
    return msgs


def probe_variants():
    base = "https://apis.data.go.kr/1741000/pharmacies"
    tries = [
        (base, {"serviceKey": SERVICE_KEY, "pageNo": 1, "numOfRows": 3}),
        (base, {"serviceKey": SERVICE_KEY, "pageNo": 1, "numOfRows": 3, "returnType": "json"}),
        (base, {"ServiceKey": SERVICE_KEY, "pageNo": 1, "numOfRows": 3, "returnType": "json"}),
        (base + "/info", {"serviceKey": SERVICE_KEY, "pageNo": 1, "numOfRows": 3, "returnType": "json"}),
        (base + "/getList", {"serviceKey": SERVICE_KEY, "pageNo": 1, "numOfRows": 3, "returnType": "json"}),
    ]
    for url, params in tries:
        shown = {k: v for k, v in params.items() if k.lower() != "servicekey"}
        print(f"--- {url} {shown}")
        try:
            q = urllib.parse.urlencode(params)
            with urllib.request.urlopen(f"{url}?{q}", timeout=60) as r:
                print("OK", r.status, r.read().decode("utf-8", "replace")[:800])
        except urllib.error.HTTPError as e:
            print("HTTP", e.code, e.read().decode("utf-8", "replace")[:600])
        except Exception as e:
            print("ERR", e)


def probe():
    probe_variants()
    for typ in CONFIG.get("endpoints", {}):
        url, extra = ep(typ)
        print(f"===== {typ} {url}")
        try:
            data = http_get_json(url, {"serviceKey": SERVICE_KEY, "pageNo": 1, "numOfRows": 3,
                                       "returnType": "json", "type": "json", **extra})
        except Exception as e:
            print("ERROR:", e)
            continue
        rows = find_rows(data)
        print("totalCount:", find_total(data))
        if rows:
            print("fields:", list(rows[0].keys()))
            for r in rows[:3]:
                print(json.dumps(r, ensure_ascii=False))
        else:
            print("raw:", json.dumps(data, ensure_ascii=False)[:1500])


def main():
    if "--probe" in sys.argv:
        return probe()
    dry = "--dry" in sys.argv
    seen = set(json.load(open(SEEN_PATH, encoding="utf-8"))) if os.path.exists(SEEN_PATH) else set()
    first_run = not os.path.exists(SEEN_PATH)
    found = collect()
    new = [x for x in found if x["id"] not in seen]
    print(f"[결과] 기간 내 {len(found)}건, 새로 알릴 {len(new)}건")
    today = datetime.now(KST).strftime("%m/%d")
    link = os.environ.get("ALERT_LINK", "https://github.com")
    if new:
        msgs = pack(new, f"🏥 서울 신규개설 {len(new)}건 ({today})")
    elif first_run:
        msgs = [f"✅ 신규개설 알림 연결 완료 ({today})\n최근 {CONFIG.get('lookback_days', 7)}일 새 인허가 없음. 내일부터 새로 생기면 알려드려요."]
    else:
        msgs = []
    if dry:
        for m in msgs:
            print("----\n" + m)
    elif msgs:
        token = kakao_token()
        for m in msgs:
            kakao_send(token, m, link)
            time.sleep(1)
        print(f"[카카오] {len(msgs)}개 메시지 발송")
    if not dry:
        seen |= {x["id"] for x in found}
        os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
        json.dump(sorted(seen), open(SEEN_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=0)


if __name__ == "__main__":
    main()
