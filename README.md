# 아파트 스마트 고지서 (vnrisa) — Home Assistant

`v.vnrisa.com` 스마트 고지서(아파트 관리비)를 매일 확인해 모든 항목을 센서로 만드는 커스텀 컴포넌트입니다.

## 기능
- 고지서의 **모든 항목을 자동으로 센서화** (항목명 하드코딩 없음, 새 항목이 생기면 자동 추가)
  - 납부하실금액·당월부과금액, 전기료/수도료/관리비 세부 항목과 소계
  - 기후환경요금·연료비조정액·각종 할인, 전기/수도 사용량, 계량기 지침
- **하루 1회** 지정 시각에 조회 (기본 09:07, 옵션에서 변경)
- 각 센서 속성에 **월별 기록(최근 24개월)** — 설치 즉시 고지서의 전월·전년동월 값으로 채워짐
- 계량기 지침을 **청구월 기준 장기 통계**(`vnrisa_bill:*`)로 기록 → 에너지 대시보드에서 해당 월에 사용량 표시
- 새 고지서가 올라오면 `vnrisa_bill_new_bill` 이벤트 발생

## 설치
### HACS
HACS → 사용자 지정 저장소 → 이 저장소 URL, 유형 **Integration** 추가 후 설치, HA 재시작.

### 수동
`custom_components/vnrisa_bill` 폴더를 HA의 `config/custom_components/`에 복사 후 재시작.

## 설정
설정 → 기기 및 서비스 → 통합 추가 → **아파트 스마트 고지서**
- **고지서 URL 또는 RoomKey**: 문자/알림으로 받은 고지서 링크를 통째로 붙여넣으면 RoomKey를 자동 추출합니다.
- **매일 확인 시각**: `HH:MM`

> RoomKey만으로 서버가 해당 호실의 최신 고지서로 리다이렉트하므로 매월 바뀌는 Token은 필요 없습니다.

## 새 고지서 알림 예시
```yaml
triggers:
  - trigger: event
    event_type: vnrisa_bill_new_bill
actions:
  - action: notify.mobile_app_my_phone
    data:
      title: "관리비 {{ trigger.event.data.month }} 고지"
      message: "{{ '{:,}'.format(trigger.event.data['납부하실금액']) }}원 · 마감 {{ trigger.event.data.due_date }}"
```

## 서비스
- `vnrisa_bill.refresh` — 예약 시각과 관계없이 즉시 조회

## 주의
고지서 페이지 HTML 구조가 바뀌면 파싱이 실패할 수 있습니다. 비공식 프로젝트이며 vnrisa와 관련이 없습니다.
