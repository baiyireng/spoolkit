class User:
    def __init__(self, first, last):
        self.first = first
        self.last = last

    def full_name(self):
        return self.first + " " + self.last
