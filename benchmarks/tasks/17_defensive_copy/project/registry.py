class Registry:
    def __init__(self):
        self._items = []

    def add(self, name):
        self._items.append(name)

    def items(self):
        return self._items
