"""kshexe/kocom_energy에서 그대로 포팅 (바이트 단위 패킷 처리라 로직을 바꾸지 않고 그대로 가져옴)."""
import struct
import hashlib


def string_to_hex(s):
    return "".join([format(ord(c), "02x") for c in s])


def hex_to_ascii(hex_string):
    bytes_obj = bytes.fromhex(hex_string)
    return bytes_obj.decode("ascii").rstrip("\x00")


def hex_to_decimal(hex_string):
    return int(hex_string, 16)


def hex_to_double(hex_string):
    """16진수 문자열을 little-endian 바이트 순서의 double 형식으로 변환"""
    byte_array = bytes.fromhex(hex_string)
    return struct.unpack("<d", byte_array)[0]


def md5_hashing(input_string):
    if not isinstance(input_string, str):
        input_string = str(input_string)
    return hashlib.md5(input_string.encode()).hexdigest()


def string_to_padded_hex(input_string, size):
    if not isinstance(input_string, str):
        input_string = str(input_string)
    hex_string = "".join(hex(ord(c))[2:] for c in input_string)
    padded_hex_string = hex_string.ljust(size, "0")
    return padded_hex_string
