from functools import partial

__all__ = (
    'dump',
    'load',
    'dumps',
    'loads',
    'json',
)

try:
    import ujson as json

    dump = partial(json.dump, escape_forward_slashes=False, ensure_ascii=False)
    load = partial(json.load)

    dumps = partial(json.dumps, escape_forward_slashes=False, ensure_ascii=False)
    loads = partial(json.loads)

except ImportError:
    try:
        import rapidjson as json

        dump = partial(json.dump, number_mode=json.NM_NATIVE, datetime_mode=json.DM_ISO8601 | json.DM_NAIVE_IS_UTC, ensure_ascii=False)
        load = partial(json.load, number_mode=json.NM_NATIVE, datetime_mode=json.DM_ISO8601 | json.DM_NAIVE_IS_UTC)

        dumps = partial(json.dumps, number_mode=json.NM_NATIVE, datetime_mode=json.DM_ISO8601 | json.DM_NAIVE_IS_UTC, ensure_ascii=False)
        loads = partial(json.loads, number_mode=json.NM_NATIVE, datetime_mode=json.DM_ISO8601 | json.DM_NAIVE_IS_UTC)

    except ImportError:
        import json

        dump = partial(json.dump, ensure_ascii=False)
        load = partial(json.load)

        dumps = partial(json.dumps, ensure_ascii=False)
        loads = partial(json.loads)


def serialize(x):
    return json.dumps(x).encode('utf-8')


def deserialize(x):
    return json.loads(x.decode('utf-8'))
