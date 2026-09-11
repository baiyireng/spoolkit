def label(name, value):
    return f"{name}={value}"


def describe(item):
    return label(item["value"], item["name"])
