def get_single_or_list(d: dict, key: str):
    value = d.get(key)
    if isinstance(value, list):
        return value
    if not value is None:
        return [value]
    return []
