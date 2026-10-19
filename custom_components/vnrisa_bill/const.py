DOMAIN = "vnrisa_bill"
PLATFORMS = ["sensor"]

CONF_ROOM_KEY = "room_key"
CONF_CHECK_TIME = "check_time"
DEFAULT_CHECK_TIME = "09:07"

BASE_URL = "https://v.vnrisa.com/Online_Bill_General.aspx?RoomKey={room_key}&GBN=011"
EVENT_NEW_BILL = f"{DOMAIN}_new_bill"
