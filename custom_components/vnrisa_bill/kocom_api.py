"""kshexe/kocom_energy의 api.py를 그대로 포팅 - 아파트 단지 월패드 서버와 TCP 소켓(포트
15000)으로 직접 통신하는 코콤 전용 바이너리 프로토콜이라, 패킷 포맷/오프셋 하나하나가
실측으로 역공학된 것들이다. 로직을 바꾸지 않고 import 경로만 이 통합에 맞게 고쳤다."""
import datetime
import asyncio
import logging
import math

from dateutil.relativedelta import relativedelta
from .kocom_util import string_to_padded_hex, string_to_hex, hex_to_ascii, hex_to_double
from .kocom_exceptions import AuthenticationError

_LOGGER = logging.getLogger(__name__)


class KocomAPI:

    # 인증 요청 패킷의 separator 필드는 월패드 펌웨어 세대에 따라 다르게 관찰되어(신형 01000000,
    # 구형 02000000), 어느 쪽이 이 기기에 맞는지 순서대로 시도해서 감지한다.
    auth_req_format = "78563412000010017c01000000000000000000000000000000000000000000000000000000000000000000000000000000000000{username}{password}{separator}{fcm}{phone}"
    auth_req = ""
    auth_separators = ("01000000", "02000000")

    # 인증 응답은 고정 헤더(8바이트) + 응답유형 코드(4바이트) + 전부 0으로 채워지는 나머지로
    # 구성된다. 나머지 0 패딩의 총 길이가 펌웨어 세대마다 달랐다(구형 32바이트, 신형 36바이트) -
    # 그 안의 값 자체는 의미가 없으므로, 패딩 길이까지 정확히 맞추기보다는 헤더와 응답유형
    # 코드만 검사한다. 이러면 아직 못 본 펌웨어의 패딩 길이 차이에도 자동으로 대응한다.
    # 응답유형 코드는 지금까지 확인된 두 값만 "성공"으로 인정하고, 모르는 값은 실패로 본다 -
    # 실패 응답이 어떤 모양인지 아직 실측한 샘플이 없어서, 헤더만 보고 무조건 통과시키는 건
    # 위험하다고 판단했다.
    auth_resp_header = "7856341201001001"
    auth_resp_ok_types = {"04000000", "08000000"}

    def _auth_response_ok(self, response: str) -> bool:
        return response[:16] == self.auth_resp_header and response[16:24] in self.auth_resp_ok_types

    # 메뉴 관련 패킷
    menu_req = "78563412b80b1001040000000000000000000000000000000000000000000000"

    # 주소 관련 패킷
    addr_req = "785634120200100120000000000000000000000000000000000000000000000018000000f00000000000000000000000000000000000000000000000"

    # 에너지 관련 패킷
    energy_disp_type = ""
    energy_req_type_1_format = "785634127800100120000000{town}0000{dong}0000{ho}000000000000{months_str}000000000000000000000000"
    energy_req_type_3_format = "785634129001100148000000{town}0000{dong}0000{ho}000000000000020000000200000001000000{months_str}00{months_str}00312c322c332c342c350000000000000000000000"

    def __init__(self, ip, username, password, fcm, phone):
        self.ip = ip
        self.port = 15000
        self.username = username
        self.password = password
        self.fcm = fcm
        self.phone = phone

        # config_flow.py에서 이미 md5+패딩 완료된 값을 받으므로 그대로 사용.
        # separator별로 요청 패킷을 미리 조립해둔다.
        self.auth_reqs = [
            self.auth_req_format.format(
                username=username,
                password=password,
                separator=separator,
                fcm=fcm,
                phone=phone,
            )
            for separator in self.auth_separators
        ]
        _LOGGER.debug(f"인증 요청 패킷 조립 결과: {self.auth_reqs}")

    async def _authenticate_socket(self):
        """알려진 separator를 순서대로 시도한다. 성공하면 그 (reader, writer)를 반환하고,
        이후 통신에 쓸 self.auth_req를 성공한 포맷으로 고정한다. 전부 실패하면 (None, None)."""
        last_response = None
        for separator, req in zip(self.auth_separators, self.auth_reqs):
            reader, writer = await asyncio.open_connection(self.ip, self.port)

            writer.write(bytes.fromhex(req))
            await writer.drain()

            try:
                auth_response = (await asyncio.wait_for(reader.read(1024), timeout=10.0)).hex()
            except asyncio.TimeoutError:
                _LOGGER.debug(f"인증 timeout (separator {separator})")
                writer.close()
                await writer.wait_closed()
                continue

            _LOGGER.debug(f"인증 응답 패킷 (separator {separator}): {auth_response}")

            if self._auth_response_ok(auth_response):
                _LOGGER.debug(f"인증 성공 (separator {separator})")
                self.auth_req = req
                return reader, writer

            last_response = auth_response
            writer.close()
            await writer.wait_closed()

        _LOGGER.error(f"인증 실패 - 알려진 separator 전부 불일치. 마지막 응답: {last_response}")
        return None, None

    async def authenticate(self):
        try:
            _LOGGER.debug(f"========== 소켓 통신 시작 ==========")
            _LOGGER.debug(f"ip : {self.ip}")
            _LOGGER.debug(f"port : {self.port}")

            reader, writer = await self._authenticate_socket()
            if reader is None:
                raise AuthenticationError("인증 정보가 올바르지 않습니다.")
            return True

        except asyncio.TimeoutError:
            _LOGGER.debug("인증 timeout")
        except AuthenticationError:
            raise
        except Exception as e:
            _LOGGER.error("소켓 통신 오류: %s", e)
        finally:
            if "writer" in locals() and writer:
                writer.close()
                await writer.wait_closed()

        return False

    async def get_energy_data(self):
        try:
            energy_response_dict = {}

            _LOGGER.debug(f"========== 소켓 통신 시작 ==========")

            reader, writer = await self._authenticate_socket()
            if reader is None:
                _LOGGER.error("인증정보가 올바르지 않습니다.")
                return {}

            writer.write(bytes.fromhex(self.menu_req))
            await writer.drain()

            try:
                menu_response = (await asyncio.wait_for(reader.read(1024), timeout=10.0)).hex()
                _LOGGER.debug(f'메뉴 정보 응답 패킷: {menu_response}')
                self.energy_disp_type = menu_response[96:100]
                _LOGGER.debug(f'에너지 조회 유형 : {self.energy_disp_type}')
            except asyncio.TimeoutError:
                _LOGGER.error("메뉴 조회 timeout")
                return {}

            writer.write(bytes.fromhex(self.addr_req))
            await writer.drain()

            try:
                addr_response = (await asyncio.wait_for(reader.read(1024), timeout=10.0)).hex()
                _LOGGER.debug(f'주소 응답 패킷: {addr_response}')
                town = addr_response[24:28]
                dong = addr_response[32:36]
                ho = addr_response[40:44]
                _LOGGER.debug(f'타운: {town}, 동: {dong}, 호: {ho}')
            except asyncio.TimeoutError:
                _LOGGER.error("주소 조회 timeout")
                return {}

            energy_req_data = ""

            if self.energy_disp_type == '0100':
                now = datetime.datetime.now()
                one_month_ago = now - relativedelta(months=1)
                two_months_ago = now - relativedelta(months=2)
                months = [
                    two_months_ago.strftime("%Y%m"),
                    one_month_ago.strftime("%Y%m"),
                    now.strftime("%Y%m"),
                ]
                months_str = ",".join(months)
                energy_req_data = self.energy_req_type_1_format.format(
                    town=town, dong=dong, ho=ho,
                    months_str=string_to_hex(months_str)
                )

            elif self.energy_disp_type == '0300':
                months_str = datetime.datetime.now().strftime("%Y-%m-00 00:00:00")
                energy_req_data = self.energy_req_type_3_format.format(
                    town=town, dong=dong, ho=ho,
                    months_str=string_to_hex(months_str)
                )

            _LOGGER.debug(f"에너지 정보 요청 패킷 : {energy_req_data}")
            writer.write(bytes.fromhex(energy_req_data))
            await writer.drain()

            try:
                # 메뉴/주소 응답은 보통 즉시 캐시에서 오지만, 에너지 응답은 월패드가
                # 계량기 쪽에서 직접 읽어와야 해서 더 걸릴 수 있다(실측상 10초로는
                # 종종 모자랐음) - 이 읽기만 넉넉하게 20초로 늘렸다.
                gnergy_response = (await asyncio.wait_for(reader.read(1024), timeout=20.0)).hex()
                _LOGGER.debug(f'에너지 정보 수신 패킷: {gnergy_response}')

                if len(gnergy_response) < 500:
                    _LOGGER.error(f"비정상 응답 데이터. 길이: {len(gnergy_response)}")
                    return {}

                if gnergy_response.startswith("7856341210"):
                    _LOGGER.error(f"잘못된 응답 헤더: {gnergy_response[:10]}")
                    return {}

                if self.energy_disp_type == '0100':
                    _LOGGER.debug(f"에너지 사용량 조회 패턴 : {self.energy_disp_type}")
                    start_idx = 64
                    energy_response_dict["two_months_ago"] = hex_to_ascii(gnergy_response[start_idx+8:start_idx+24])
                    energy_response_dict["electricity_usage_two_months_ago"] = hex_to_double(gnergy_response[start_idx+40:start_idx+56])
                    start_idx = 120
                    energy_response_dict["gas_usage_two_months_ago"] = hex_to_double(gnergy_response[start_idx+40:start_idx+56])
                    start_idx = 176
                    energy_response_dict["water_usage_two_months_ago"] = hex_to_double(gnergy_response[start_idx+40:start_idx+56])
                    start_idx = 232
                    energy_response_dict["hot_water_usage_two_months_ago"] = hex_to_double(gnergy_response[start_idx+40:start_idx+56])
                    start_idx = 288
                    energy_response_dict["heating_usage_two_months_ago"] = hex_to_double(gnergy_response[start_idx+40:start_idx+56])

                    start_idx = 344
                    energy_response_dict["last_month"] = hex_to_ascii(gnergy_response[start_idx+8:start_idx+24])
                    energy_response_dict["electricity_usage_last_month"] = hex_to_double(gnergy_response[start_idx+40:start_idx+56])
                    start_idx = 400
                    energy_response_dict["gas_usage_last_month"] = hex_to_double(gnergy_response[start_idx+40:start_idx+56])
                    start_idx = 456
                    energy_response_dict["water_usage_last_month"] = hex_to_double(gnergy_response[start_idx+40:start_idx+56])
                    start_idx = 512
                    energy_response_dict["hot_water_usage_last_month"] = hex_to_double(gnergy_response[start_idx+40:start_idx+56])
                    start_idx = 568
                    energy_response_dict["heating_usage_last_month"] = hex_to_double(gnergy_response[start_idx+40:start_idx+56])

                    start_idx = 624
                    energy_response_dict["this_month"] = hex_to_ascii(gnergy_response[start_idx+8:start_idx+24])
                    energy_response_dict["electricity_usage_this_month"] = hex_to_double(gnergy_response[start_idx+40:start_idx+56])
                    start_idx = 680
                    energy_response_dict["gas_usage_this_month"] = hex_to_double(gnergy_response[start_idx+40:start_idx+56])
                    start_idx = 736
                    energy_response_dict["water_usage_this_month"] = hex_to_double(gnergy_response[start_idx+40:start_idx+56])
                    start_idx = 792
                    energy_response_dict["hot_water_usage_this_month"] = hex_to_double(gnergy_response[start_idx+40:start_idx+56])
                    start_idx = 848
                    energy_response_dict["heating_usage_this_month"] = hex_to_double(gnergy_response[start_idx+40:start_idx+56])

                elif self.energy_disp_type == '0300':
                    _LOGGER.debug(f"에너지 사용량 조회 패턴 : {self.energy_disp_type}")
                    start_idx = 184
                    energy_response_dict["this_month"] = hex_to_ascii(gnergy_response[start_idx+8:start_idx+22])
                    energy_response_dict["electricity_usage_this_month"] = hex_to_double(gnergy_response[start_idx+48:start_idx+64])
                    start_idx = 272
                    energy_response_dict["water_usage_this_month"] = hex_to_double(gnergy_response[start_idx+48:start_idx+64])
                    start_idx = 360
                    energy_response_dict["hot_water_usage_this_month"] = hex_to_double(gnergy_response[start_idx+48:start_idx+64])
                    start_idx = 448
                    energy_response_dict["gas_usage_this_month"] = hex_to_double(gnergy_response[start_idx+48:start_idx+64])
                    start_idx = 536
                    energy_response_dict["heating_usage_this_month"] = hex_to_double(gnergy_response[start_idx+48:start_idx+64])

            except asyncio.TimeoutError:
                _LOGGER.error("에너지 응답 timeout")
                return {}

            _LOGGER.debug(f"========== 소켓 통신 종료 ==========")
            return energy_response_dict

        except Exception as e:
            _LOGGER.error(f"소켓 통신 오류: {e}")
            return {}
        finally:
            if "writer" in locals() and writer:
                writer.close()
                await writer.wait_closed()
