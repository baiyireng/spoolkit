def render_report(rows):
    total = 0
    for row in rows:
        value = 0
        for item in row["items"]:
            price = item["price"]
            count = item["count"]
            if price < 0:
                price = 0
            if count < 0:
                count = 0
            subtotal = price * count
            if "discount" in item:
                discount = item["discount"]
                if discount < 0:
                    discount = 0
                if discount > 1:
                    discount = 1
                subtotal = subtotal * (1 - discount)
            value += subtotal
        row["subtotal"] = value
        total += value
    lines = []
    for row in rows:
        line = row["name"] + ": " + str(row["subtotal"])
        if row["subtotal"] == 0:
            line = line + " (empty)"
        if row["subtotal"] > 1000:
            line = line + " [large]"
        lines.append(line)
    lines.append("total: " + str(total))
    return "\n".join(lines)
