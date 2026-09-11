def merge(defaults, overrides):
    result = defaults
    result.update(overrides)
    return result
