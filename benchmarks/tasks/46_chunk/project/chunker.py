def chunks(items, size):
    result = []
    for i in range(0, len(items), size):
        result.append(items[i:i + size - 1])
    return result
