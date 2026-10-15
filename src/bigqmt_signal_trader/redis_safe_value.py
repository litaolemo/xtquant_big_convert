# coding: utf-8
"""The identity codec shared by persisted identities and Lua remark replies.

QMT's bundled Redis checks the whole response for stock/operation digit
patterns (#393). Encoding must happen before the response reaches Python.
Keep this module dependency-free: exec_events already imports order_remarks.
"""

import json


PREFIX = "bqidentity:"
_DIGIT_ENCODE = str.maketrans("0123456789", "ghijklmnop")
_DIGIT_DECODE = str.maketrans("ghijklmnop", "0123456789")

# Encode original bytes, not a cjson reserialization (which can lose precision).
LUA_ENCODE = """
local function safe_encode(raw, prefix)
    local alphabet = 'ghijklmnopabcdef'
    local parts = {prefix}
    for i = 1, #raw do
        local value = string.byte(raw, i)
        local high = math.floor(value / 16) + 1
        local low = value % 16 + 1
        parts[#parts + 1] = string.sub(alphabet, high, high)
        parts[#parts + 1] = string.sub(alphabet, low, low)
    end
    return table.concat(parts)
end
"""


def encode_json(value):
    raw = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
    return PREFIX + raw.hex().translate(_DIGIT_ENCODE)


def decode_json(raw):
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
    if text.startswith(PREFIX):
        token = text[len(PREFIX):]
        text = bytes.fromhex(token.translate(_DIGIT_DECODE)).decode("utf-8")
    return json.loads(text)
