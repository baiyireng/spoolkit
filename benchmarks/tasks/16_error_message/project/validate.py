def positive(value):
    if value <= 0:
        raise ValueError("必须是正数")
    return value
