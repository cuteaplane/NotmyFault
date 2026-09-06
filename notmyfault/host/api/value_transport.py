from fastapi.responses import JSONResponse

from notmyfault.core.data_types import DataTypeError
from notmyfault.core.value_codec import decode_value, encode_value


ENCODING_HEADER = "X-NMF-Value-Encoding"


def decode_request(request, body):
    encoding = request.headers.get(ENCODING_HEADER)
    if encoding is None:
        return body
    if encoding != "typed-v1":
        raise DataTypeError("数据编码版本不受支持", code="invalid_encoding")
    return decode_value(body)


async def read_value_json(request):
    return decode_request(request, await request.json())


def value_response(value, status_code=200):
    return JSONResponse(encode_value(value), status_code=status_code, headers={ENCODING_HEADER: "typed-v1"})
