import re


def find_all(text, word):
    return re.findall(word, text)
