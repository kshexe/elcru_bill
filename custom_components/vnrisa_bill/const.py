"""엘크루 관리비 - 관리비(vnrisa) + 도시가스(GSE) + 음식물쓰레기(citywaste) + 코콤 상수.

v.vnrisa.com의 고지서 링크는 RoomKey만으로는 못 쓴다 - YYYYMM별로 서버가 새로 발급하는
Token이 정확히 일치해야 하고(실측 확인됨, 2026-10-19), 그 Token은 카카오 알림톡으로만
오며 예측도 재사용도 불가능하다. 그래서 "RoomKey 등록 후 자동으로 매달 재조회"하는 구조는
근본적으로 성립하지 않는다 - 매달 새 링크를 사람이 직접 넣어줘야 한다. 그래서 관리비
쪽은 복잡한 config flow/Token 저장 대신, 텍스트 입력 엔티티 하나에 그 달의 링크를
그대로 붙여넣으면 그 즉시 조회해서 센서에 반영하는 가장 단순한 구조로 간다.

도시가스(GSE)는 반대로 아이디/비밀번호 로그인이라 Token 문제가 없다 - aptner_parking/
parkingweb/myhome_parking과 같은 패턴으로, 로그인 정보는 config flow(최초 설정)에서
한 번 입력받아 저장하고, 조회 주기는 Options flow에서 바꿀 수 있게 한다.

citywaste(RFID 음쓰(음식물쓰레기) 종량기)도 GSE와 같은 아이디/비밀번호 로그인이라 Token
문제가 없다. 다만 GSE처럼 전체 기간을 한 번에 주는 API가 없어 달마다 따로 조회해야
해서(citywaste.py), 매일이 아니라 매달 1일에만 확인한다 - 이미 끝난 달 정산내역은
그 뒤로 안 바뀌니 매일 다시 긁을 이유가 없다.

코콤(kocom_energy, kshexe 포크)도 같은 이유로 합쳤다 - 아파트 단지 월패드에
직접 붙는 로컬 소켓 통신이라 Token도 없고, 전기/가스/수도 사용량을 따로 보여주던
센서를 하나의 통합(기기)으로 모아서 관리하기 위함. 소켓 프로토콜 자체(kocom_api.py/
kocom_util.py)는 원본 그대로 포팅 - 바이트 오프라인 파싱 로직이라 손대지 않았다.
"""

DOMAIN = "vnrisa_bill"
PLATFORMS = ["sensor", "text"]

CONF_GAS_MEMBER_ID = "gas_member_id"
CONF_GAS_MEMBER_PWD = "gas_member_pwd"
CONF_GAS_CHECK_TIME = "gas_check_time"
DEFAULT_GAS_CHECK_TIME = "09:00"

CONF_CITYWASTE_ID = "citywaste_id"
CONF_CITYWASTE_PWD = "citywaste_pwd"
DEFAULT_CITYWASTE_CHECK_TIME = "09:00"

# 정산내역(월 1회, 위 CHECK_TIME)과 분리된 실시간 조회(로그인 없는 API) 전용 주기 -
# staiji/citywaste_korea가 쓰는 고정 30분, oukene/citywaste의 옵션 화면 기본값과도
# 같다(oukene 최초 설치 폼에만 60분이 있었는데 그건 Required()에 default를 잘못
# 넘긴 버그로 보임 - staiji 쪽처럼 30분을 정상값으로 봄).
CONF_CITYWASTE_REFRESH_INTERVAL = "citywaste_refresh_interval"
DEFAULT_CITYWASTE_REFRESH_INTERVAL = 1800

# 실시간 조회(로그인 없는 API)에 필요한 태그인쇄번호/동/호 - 아이디/비밀번호와 똑같이
# "음쓰 설정"에서 사용자가 직접 입력하는 필수값이다. 자동 추출/자동 채움은 없고,
# 여기 저장된 값만 그대로 쓴다(__init__.py의 async_refresh_citywaste).
CONF_CITYWASTE_TAG_ID = "citywaste_tag_id"
CONF_CITYWASTE_DONG = "citywaste_dong"
CONF_CITYWASTE_HO = "citywaste_ho"

CONF_KOCOM_USERNAME = "kocom_username"
CONF_KOCOM_PASSWORD = "kocom_password"
CONF_KOCOM_IP = "kocom_ip"
CONF_KOCOM_UPDATE_INTERVAL = "kocom_update_interval"
DEFAULT_KOCOM_UPDATE_INTERVAL = 1800
